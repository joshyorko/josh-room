from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from josh_room import room_store_hauler as component
from josh_room import room_store_hauler_runner as runner
from josh_room.cancellation import CLICancelled

REPOSITORY_ID = "a" * 64
SNAPSHOT_ID = "b" * 64
TREE_ID = "c" * 64
IMAGE_DIGEST = "sha256:" + "d" * 64
FILE_DIGEST = "sha256:" + "e" * 64
JAT_ARTIFACT = "sha256:" + "a" * 64


class _Restic:
    def __init__(self):
        self.backups = []

    def backup(self, source, *, parent=None, cancellation=None):
        source = Path(source)
        self.backups.append((source, parent, sorted(path.name for path in source.iterdir())))
        return SimpleNamespace(snapshot_id="f" * 64)

    def snapshot(self, snapshot_id):
        return SimpleNamespace(tree_id=TREE_ID)


class _Hauler:
    def __init__(self, inventory=None):
        self.rows = list(inventory or [])
        self.calls = []
        self.archive_bytes = b"synthetic native Hauler archive"

    @staticmethod
    def _touch_store(store):
        Path(store).mkdir(parents=True, exist_ok=True)

    def sync_image_txt(self, store, temp, sources, **kwargs):
        self._touch_store(store)
        selected = Path(sources[0]).read_text(encoding="utf-8").splitlines()
        self.calls.append(("images", selected, kwargs))
        self.rows.extend(
            {"Reference": image, "Type": "image", "Digest": image.partition("@")[2] or IMAGE_DIGEST}
            for image in selected
        )

    def sync(self, store, temp, *manifests, **kwargs):
        self._touch_store(store)
        self.calls.append(("manifests", tuple(Path(path).name for path in manifests), kwargs))
        for manifest in manifests:
            text = Path(manifest).read_text(encoding="utf-8")
            for line in text.splitlines():
                if "image:" in line:
                    image = line.split("image:", 1)[1].strip().strip("'\"")
                    self.rows.append({"Reference": image, "Type": "image", "Digest": IMAGE_DIGEST})

    def sync_files(self, store, temp, files, **kwargs):
        self._touch_store(store)
        self.calls.append(("files", tuple(name for _path, name in files), kwargs))
        self.rows.extend({"Reference": name, "Type": "file", "Digest": FILE_DIGEST} for _path, name in files)
        self.rows.extend({"Reference": name, "Type": "image", "Digest": IMAGE_DIGEST} for name in kwargs.get("images", ()))

    def inventory(self, store, temp):
        self.calls.append(("inventory",))
        return list(self.rows)

    def save(self, store, temp, output, **kwargs):
        self.calls.append(("save",))
        Path(output).write_bytes(self.archive_bytes)


class _Cancellation:
    cancelled = False


def _prior(*, refs=None, source_hash=None, hauler_version="2.1.1"):
    return {
        "kind": "hauler-content",
        "snapshot": {
            "repository_id": REPOSITORY_ID,
            "repository_format": 2,
            "snapshot_id": SNAPSHOT_ID,
            "tree_id": TREE_ID,
        },
        "archive_sha256": "1" * 64,
        "archive_size": 1,
        "member_basename": "hauler-content.tar.zst",
        "references": refs or [{"digest": IMAGE_DIGEST, "kind": "image"}],
        "source_input_sha256": source_hash,
        "hauler_version": hauler_version,
    }


def _capture(tmp_path, *, hauler=None, restic=None, **kwargs):
    return component.capture_hauler_component(
        workspace=tmp_path,
        prior_component=kwargs.pop("prior_component", None),
        repository_id=kwargs.pop("repository_id", REPOSITORY_ID),
        repository_format=kwargs.pop("repository_format", 2),
        restic=restic or _Restic(),
        hauler=hauler or _Hauler(),
        hauler_version=kwargs.pop("hauler_version", "2.1.1"),
        **kwargs,
    )


def test_capture_retains_alias_and_platform_identity():
    rows = [
        {"Reference": "registry.example.test/first:latest", "Platform": "linux/amd64", "Digest": IMAGE_DIGEST, "Type": "image"},
        {"Reference": "registry.example.test/second:latest", "Platform": "linux/amd64", "Digest": IMAGE_DIGEST, "Type": "image"},
    ]

    result = component._native_references(rows)

    assert len(result) == 2
    assert {row["reference_sha256"] for row in result} == {hashlib.sha256(row["Reference"].encode()).hexdigest() for row in rows}
    assert all(row["platform_sha256"] == hashlib.sha256(b"linux/amd64").hexdigest() for row in result)


def test_capture_rejects_reserved_jat_anchor_before_backup(tmp_path):
    source = tmp_path / "extra"
    source.write_bytes(b"synthetic")
    restic = _Restic()
    hauler = _Hauler([{ "Reference": "hauler/rcc-environment.rcca:latest", "Type": "file", "Digest": FILE_DIGEST }])
    with pytest.raises(component.RoomStoreHaulerError, match="reserved"):
        _capture(tmp_path, hauler=hauler, restic=restic, files=[(source, "extra")])
    assert restic.backups == []


def test_captured_hauler_archive_respects_portable_input_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(component, "MAX_ARCHIVE_BYTES", 4, raising=False)
    source = tmp_path / "extra"
    source.write_bytes(b"synthetic")
    restic = _Restic()
    with pytest.raises(component.RoomStoreHaulerError, match="export limit"):
        _capture(tmp_path, restic=restic, files=[(source, "extra")])
    assert restic.backups == []


def test_hauler_reads_private_frozen_file_bytes(tmp_path):
    original = tmp_path / "input.txt"
    original.write_bytes(b"expected frozen bytes")
    initial = original.stat()
    class ConcurrentEditHauler(_Hauler):
        def sync_files(self, store, temp, files, **kwargs):
            original.write_bytes(b"concurrent other bytes")
            try:
                assert Path(files[0][0]).read_bytes() == b"expected frozen bytes"
                assert Path(files[0][0]) != original
                super().sync_files(store, temp, files, **kwargs)
            finally:
                original.write_bytes(b"expected frozen bytes")
                os.utime(original, ns=(initial.st_atime_ns, initial.st_mtime_ns))
    result = _capture(tmp_path, hauler=ConcurrentEditHauler(), files=[(original, "input.txt")])
    assert result["kind"] == "hauler-content"


def test_local_image_identity_reuses_saved_component_without_recapture(tmp_path):
    hauler = _Hauler()
    restic = _Restic()
    local = [("synthetic:latest", "sha256:" + "1" * 64)]
    first = _capture(tmp_path, hauler=hauler, restic=restic, local_images=local)
    assert any(call[0] == "files" and call[2]["images"] == ["synthetic:latest"] for call in hauler.calls)
    before = (len(hauler.calls), len(restic.backups))

    unchanged = _capture(tmp_path, hauler=hauler, restic=restic, local_images=local, prior_component=first)

    assert unchanged == first
    assert (len(hauler.calls), len(restic.backups)) == before
    changed = _capture(tmp_path, hauler=hauler, restic=restic,
                       local_images=[("synthetic:latest", "sha256:" + "2" * 64)], prior_component=first)
    assert changed["source_input_sha256"] != first["source_input_sha256"]
    assert len(restic.backups) == before[1] + 1


def test_managed_local_images_delegate_to_jat_native_local_capture(tmp_path):
    adapter = object.__new__(runner.ManagedHaulerAdapter)
    calls = []
    adapter._invoke = lambda operation, request: calls.append((operation, request))

    adapter.sync_files(tmp_path / "store", tmp_path / "temp", [], images=["synthetic:latest"])

    assert calls[0][0] == "sync_files"
    assert calls[0][1]["images"] == ["synthetic:latest"]


def test_pinned_image_reuses_prior_without_hauler_or_restic(tmp_path):
    image = "registry.example/team/app@" + IMAGE_DIGEST
    hauler = _Hauler()
    restic = _Restic()
    prior = _prior(source_hash=component._source_identity(
        images=[image], manifests=[], files=[], hauler_version="2.1.1"
    )[0])
    result = _capture(
        tmp_path,
        hauler=hauler,
        restic=restic,
        prior_component=prior,
        requested_images=[image],
    )
    assert result == prior
    assert hauler.calls == []
    assert restic.backups == []


def test_mutable_image_resolves_natively_then_reuses_matching_digest(tmp_path):
    image = "registry.example/team/app:stable"
    hauler = _Hauler([{"Reference": image, "Type": "image", "Digest": IMAGE_DIGEST}])
    restic = _Restic()
    digest = component._source_identity(
        images=[image], manifests=[], files=[], hauler_version="2.1.1"
    )[0]
    prior = _prior(source_hash=digest, refs=[{"digest": IMAGE_DIGEST, "kind": "image", "reference_sha256": hashlib.sha256(image.encode()).hexdigest()}])
    result = _capture(tmp_path, hauler=hauler, restic=restic, prior_component=prior, requested_images=[image])
    assert result == prior
    assert [call[0] for call in hauler.calls] == ["images", "inventory"]
    assert restic.backups == []


def test_capture_saves_only_requested_content_archive_and_native_references(tmp_path):
    image = "registry.example/team/app@" + IMAGE_DIGEST
    file_path = tmp_path / "fixture.bin"
    file_path.write_bytes(b"synthetic file")
    hauler = _Hauler()
    restic = _Restic()
    result = _capture(
        tmp_path,
        hauler=hauler,
        restic=restic,
        requested_images=[image],
        files=[(file_path, "fixture.bin")],
    )
    assert result["references"] == [
        {"digest": IMAGE_DIGEST, "kind": "image", "reference_sha256": hashlib.sha256(image.encode()).hexdigest()},
        {"digest": FILE_DIGEST, "kind": "file", "reference_sha256": hashlib.sha256(b"fixture.bin").hexdigest()},
    ]
    assert result["hauler_version"] == "2.1.1"
    assert result["source_input_sha256"]
    assert result["archive_size"] == len(hauler.archive_bytes)
    assert restic.backups[0][1] is None
    assert restic.backups[0][2] == ["hauler-content.tar.zst", "metadata.json"]
    assert [call[0] for call in hauler.calls] == ["images", "files", "inventory", "save"]
    assert "registry.example" not in repr(result)
    assert str(file_path) not in repr(result)


def test_native_media_type_is_preserved_without_inference(tmp_path):
    hauler = _Hauler([{
        "Reference": "chart:latest",
        "Type": "chart",
        "Digest": IMAGE_DIGEST,
        "MediaType": "application/vnd.example.chart.v1",
    }])
    result = _capture(tmp_path, hauler=hauler, manifests=[_manifest(tmp_path)])
    assert result["references"] == [{
        "digest": IMAGE_DIGEST,
        "kind": "chart",
        "media_type": "application/vnd.example.chart.v1",
        "reference_sha256": hashlib.sha256(b"chart:latest").hexdigest(),
    }]


def test_missing_native_digest_or_type_fails_closed(tmp_path):
    hauler = _Hauler([{"Reference": "app:latest", "Type": "image"}])
    with pytest.raises(component.RoomStoreHaulerError, match="immutable digest evidence"):
        _capture(tmp_path, hauler=hauler, requested_images=["app:latest"])


def test_wrong_native_digest_for_pinned_image_fails_closed(tmp_path):
    image = "app@" + IMAGE_DIGEST
    hauler = _Hauler([{"Reference": image, "Type": "image", "Digest": FILE_DIGEST}])
    with pytest.raises(component.RoomStoreHaulerError, match="digest did not match"):
        _capture(tmp_path, hauler=hauler, requested_images=[image])


def test_wrong_repository_prior_fails_closed(tmp_path):
    prior = _prior(source_hash="a" * 64)
    prior["snapshot"]["repository_id"] = "0" * 64
    with pytest.raises(component.RoomStoreHaulerError, match="another Room Store"):
        _capture(tmp_path, prior_component=prior, requested_images=["app:latest"])


def test_cancellation_after_acquisition_cleans_private_stage(tmp_path):
    image = "app:latest"
    token = _Cancellation()
    private_paths = []

    class CancellingHauler(_Hauler):
        def sync_image_txt(self, store, temp, sources, **kwargs):
            private_paths.extend([Path(store).parent, Path(sources[0]).parent])
            super().sync_image_txt(store, temp, sources, **kwargs)
            token.cancelled = True

    with pytest.raises(component.RoomStoreHaulerError, match="cancelled"):
        _capture(tmp_path, hauler=CancellingHauler(), requested_images=[image], cancellation=token)
    assert private_paths
    assert all(not path.exists() for path in private_paths)


def test_changed_file_during_acquisition_blocks_archive_and_backup(tmp_path):
    source = tmp_path / "asset.bin"
    source.write_bytes(b"before")
    restic = _Restic()

    class RacingHauler(_Hauler):
        def sync_files(self, store, temp, files, **kwargs):
            super().sync_files(store, temp, files, **kwargs)
            source.write_bytes(b"after")

    with pytest.raises(component.RoomStoreHaulerError, match="changed during capture"):
        _capture(tmp_path, hauler=RacingHauler(), restic=restic, files=[(source, "asset.bin")])
    assert restic.backups == []


def test_empty_selection_is_no_component(tmp_path):
    hauler = _Hauler()
    assert _capture(tmp_path, hauler=hauler) is None
    assert hauler.calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner/mode protection")
def test_selected_owned_rcc_home_is_secured_before_component_execution(tmp_path, monkeypatch):
    root = tmp_path / "jat"
    root.mkdir()
    home = tmp_path / "rcc-home"
    home.mkdir(mode=0o750)
    monkeypatch.setattr(runner.jat, "_managed_runtime", lambda _root: (
        "/synthetic/rcc", JAT_ARTIFACT, {"ROBOCORP_HOME": str(home)},
    ))

    runner._require_managed_runtime(root)

    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_managed_adapter_factory_observes_hauler_inside_selected_artifact(tmp_path, monkeypatch):
    calls = []
    private_home = tmp_path / "rcc-home"
    private_home.mkdir(mode=0o700)
    runner.private_paths.protect_private_directory(private_home)

    def run(argv, timeout, *, cwd, env):
        calls.append((argv, timeout, cwd, dict(env)))
        receipt = Path(argv[argv.index("--receipt-file") + 1])
        receipt.write_text(json.dumps({"artifactDigest": JAT_ARTIFACT, "exitCode": 0}), encoding="utf-8")
        return 0, "hauler version v2.1.1\n", ""

    monkeypatch.setattr(
        runner.jat,
        "_managed_runtime",
        lambda _root: ("/managed/rcc", JAT_ARTIFACT, {"PYTHONPATH": "/jat/src", "PATH": "/managed/bin", "ROBOCORP_HOME": str(private_home)}),
    )
    monkeypatch.setattr(runner.jat, "_run_cli", run)
    monkeypatch.setattr(runner.jat, "_validate_rcc_receipt", lambda path, artifact, status: None)
    adapter = runner.create_managed_hauler_adapter(tmp_path)
    assert adapter.hauler_version == "v2.1.1"
    argv, timeout, cwd, env = calls[0]
    assert argv[:6] == ["/managed/rcc", "--no-build", "env", "exec", "--artifact", JAT_ARTIFACT]
    assert "hauler" not in argv
    assert argv[-3:] == ["python", "-c", runner._HAULER_VERSION_PROBE]
    assert timeout <= 120
    assert cwd == tmp_path.resolve()
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(Path(runner.__file__).resolve().parent.parent)


def test_managed_operation_writes_private_closed_request_and_validates_receipts(tmp_path, monkeypatch):
    adapter = runner.ManagedHaulerAdapter.__new__(runner.ManagedHaulerAdapter)
    adapter.jat_root = tmp_path
    adapter.cancellation = _Cancellation()
    adapter.executable = "/managed/rcc"
    adapter.artifact = JAT_ARTIFACT
    adapter.environment = {"PATH": "/managed/bin"}
    adapter.timeout = 12
    captured = {}
    private_path_events = []
    for helper_name in ("protect_private_directory", "verify_private_path", "secure_private_file", "protect_private_file"):
        original = getattr(runner.private_paths, helper_name)

        def wrapped(*args, _name=helper_name, _original=original, **kwargs):
            private_path_events.append((_name, args[0]))
            return _original(*args, **kwargs)

        monkeypatch.setattr(runner.private_paths, helper_name, wrapped)

    def run(argv, timeout, *, cwd, env):
        captured["argv"] = argv
        captured["timeout"] = timeout
        request = Path(argv[argv.index("--request-file") + 1])
        result = Path(argv[argv.index("--result-file") + 1])
        captured["request"] = json.loads(request.read_text(encoding="utf-8"))
        captured["request_mode"] = stat.S_IMODE(request.stat().st_mode)
        captured["private_root"] = request.parent
        result.write_text(json.dumps({
            "format_version": 1,
            "operation": "inventory",
            "success": True,
            "exit_status": 0,
            "value": [{"Reference": "private/app:tag", "Type": "image", "Digest": IMAGE_DIGEST}],
            "error": None,
        }), encoding="utf-8")
        receipt = Path(argv[argv.index("--receipt-file") + 1])
        receipt.write_text("{}", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(runner.jat, "_run_cli", run)
    monkeypatch.setattr(runner.jat, "_validate_rcc_receipt", lambda path, artifact, status: None)
    result = adapter.inventory(tmp_path / "store", tmp_path / "temp")
    assert result == [{"Reference": "private/app:tag", "Type": "image", "Digest": IMAGE_DIGEST}]
    assert captured["request"] == {
        "format_version": 1,
        "operation": "inventory",
        "store": str(tmp_path / "store"),
        "temp": str(tmp_path / "temp"),
        "check": False,
    }
    assert captured["request_mode"] == 0o600
    assert captured["timeout"] == 12
    assert captured["argv"][1:5] == ["--no-build", "env", "exec", "--artifact"]
    assert "--artifact" in captured["argv"] and "--no-build" in captured["argv"]
    assert not captured["private_root"].exists()
    assert {name for name, _path in private_path_events} == {
        "protect_private_directory",
        "verify_private_path",
        "secure_private_file",
        "protect_private_file",
    }


def test_managed_runner_uses_windows_private_acl_helpers(tmp_path, monkeypatch):
    owner = "S-1-5-21-1000-2000-3000-1001"

    class FakeWindowsSecurity:
        def __init__(self):
            self.applied = []

        def current_user_sid(self):
            return owner

        def apply_private_acl(self, path, owner_sid, *, directory):
            self.applied.append((Path(path), directory))

        def read_security(self, path):
            directory = Path(path).is_dir()
            return owner, True, tuple(runner.private_paths._expected_aces(owner, directory=directory))

    security = FakeWindowsSecurity()
    monkeypatch.setattr(runner.private_paths, "_is_windows", lambda: True)
    monkeypatch.setattr(runner.private_paths, "_windows_api", lambda: security)
    adapter = runner.ManagedHaulerAdapter.__new__(runner.ManagedHaulerAdapter)
    adapter.jat_root = tmp_path
    adapter.cancellation = _Cancellation()
    adapter.executable = "/managed/rcc"
    adapter.artifact = JAT_ARTIFACT
    adapter.environment = {}
    adapter.timeout = 12

    def run(argv, timeout, *, cwd, env):
        result = Path(argv[argv.index("--result-file") + 1])
        result.write_text(json.dumps({
            "format_version": 1,
            "operation": "inventory",
            "success": True,
            "exit_status": 0,
            "value": [{"Reference": "private/app:tag", "Type": "image", "Digest": IMAGE_DIGEST}],
            "error": None,
        }), encoding="utf-8")
        receipt = Path(argv[argv.index("--receipt-file") + 1])
        receipt.write_text("{}", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(runner.jat, "_run_cli", run)
    monkeypatch.setattr(runner.jat, "_validate_rcc_receipt", lambda path, artifact, status: None)
    assert adapter.inventory(tmp_path / "store", tmp_path / "temp")
    assert any(directory for _path, directory in security.applied)
    assert any(not directory for _path, directory in security.applied)


def test_managed_adapter_propagates_cancellation_after_owned_process_cleanup(tmp_path, monkeypatch):
    adapter = runner.ManagedHaulerAdapter.__new__(runner.ManagedHaulerAdapter)
    adapter.jat_root = tmp_path
    adapter.cancellation = _Cancellation()
    adapter.executable = "/managed/rcc"
    adapter.artifact = JAT_ARTIFACT
    adapter.environment = {}
    adapter.timeout = 12
    paths = []

    def run(argv, timeout, *, cwd, env):
        paths.append(Path(argv[argv.index("--request-file") + 1]).parent)
        raise CLICancelled()

    monkeypatch.setattr(runner.jat, "_run_cli", run)
    with pytest.raises(CLICancelled):
        adapter._invoke("inventory", {"store": str(tmp_path / "store"), "temp": str(tmp_path / "temp"), "check": False})
    assert paths and not paths[0].exists()


@pytest.mark.parametrize(
    "body",
    [
        {"format_version": 1, "operation": "unknown"},
        {"format_version": 1, "operation": "inventory", "store": "s", "temp": "t", "check": False, "extra": 1},
        {"format_version": 1, "operation": "inventory", "store": "s", "temp": "t", "check": "false"},
    ],
)
def test_worker_rejects_unknown_or_malformed_requests(tmp_path, body):
    runner.private_paths.protect_private_directory(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(body), encoding="utf-8")
    runner.private_paths.protect_private_file(request)
    with pytest.raises(runner.ManagedHaulerError):
        runner._worker_request(request)


def test_worker_dispatches_native_adapter_without_docker_local_images(monkeypatch, tmp_path):
    calls = []

    class FakeAdapter:
        def __init__(self, process_runner, timeout):
            calls.append(("init", process_runner, timeout))

        def sync_files(self, store, temp, files, **kwargs):
            calls.append(("sync_files", store, temp, files, kwargs))

    process_module = ModuleType("jat.process")
    process_module.ProcessRunner = lambda: "native-process-runner"
    hauler_module = ModuleType("jat.hauler")
    hauler_module.HaulerAdapter = FakeAdapter
    monkeypatch.setitem(sys.modules, "jat.process", process_module)
    monkeypatch.setitem(sys.modules, "jat.hauler", hauler_module)
    value = runner._worker_value({
        "operation": "sync_files",
        "store": str(tmp_path / "store"),
        "temp": str(tmp_path / "temp"),
        "files": [[str(tmp_path / "payload"), "payload.bin"]],
        "retries": None,
        "exclude_extras": False,
    })
    assert value is None
    call = calls[-1]
    assert call[0] == "sync_files"
    assert call[3] == [(tmp_path / "payload", "payload.bin")]
    assert "images" not in call[4]


def test_worker_acquires_and_verifies_rcc_through_native_jat_adapter(monkeypatch, tmp_path):
    root = tmp_path / "private-component"
    root.mkdir(mode=0o700)
    runner.private_paths.protect_private_directory(root)
    archive = root / "rcc-environment.rcca"
    archive.write_bytes(b"synthetic rcca")
    runner.private_paths.protect_private_file(archive)
    metadata = root / "metadata.json"
    payload = {
        "format_version": 1,
        "source_input_sha256": "a" * 64,
        "artifact_digest": "sha256:" + "1" * 64,
        "specification_digest": "sha256:" + "2" * 64,
        "legacy_blueprint_key": "synthetic-legacy-key",
        "archive_sha256": hashlib.sha256(b"synthetic rcca").hexdigest(),
        "archive_size": len(b"synthetic rcca"),
        "rcc_version": "v18.19.5",
        "platform": "linux-x64",
        "robot_relative_path": "robot.yaml",
    }
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    runner.private_paths.protect_private_file(metadata)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    robot_file = workspace / "robot.yaml"
    robot_file.write_text("tasks: {}\n", encoding="utf-8")
    private_home = tmp_path / "rcc-home"
    private_home.mkdir(mode=0o700)
    runner.private_paths.protect_private_directory(private_home)
    monkeypatch.setenv("ROBOCORP_HOME", str(private_home))
    monkeypatch.setenv("JOSH_ROOM_RCC_EXE", "/managed/rcc")
    calls = []

    class FakeEnvironmentMetadata:
        @classmethod
        def model_validate(cls, value):
            calls.append(("metadata", value))
            return SimpleNamespace(**value)

    class FakeRCCArtifactAdapter:
        def __init__(self, process_runner, *, executable, timeout):
            calls.append(("adapter", process_runner, executable, timeout))

        def acquire(self, *args, **kwargs):
            calls.append(("acquire", args, kwargs))
            return SimpleNamespace(
                artifact=payload["artifact_digest"],
                specification_digest=payload["specification_digest"],
                legacy_blueprint_key=payload["legacy_blueprint_key"],
                platform="linux_amd64",
            )

        def verify(self, robot):
            calls.append(("verify", robot))

    models = ModuleType("jat.models")
    models.EnvironmentArtifactMetadata = FakeEnvironmentMetadata
    process = ModuleType("jat.process")
    process.ProcessRunner = lambda: "native-process-runner"
    rcc_artifacts = ModuleType("jat.rcc_artifacts")
    rcc_artifacts.RCCArtifactAdapter = FakeRCCArtifactAdapter
    rcc_artifacts.EXPECTED_RCC_VERSION = "v18.19.5"
    services = ModuleType("jat.services")
    services._source_robot_path = lambda root, relative: Path(root) / relative
    monkeypatch.setitem(sys.modules, "jat.models", models)
    monkeypatch.setitem(sys.modules, "jat.process", process)
    monkeypatch.setitem(sys.modules, "jat.rcc_artifacts", rcc_artifacts)
    monkeypatch.setitem(sys.modules, "jat.services", services)
    result = runner._worker_value({
        "operation": "acquire_rcc",
        "archive": str(archive),
        "metadata": str(metadata),
        "workspace": str(workspace),
        "robot_file": str(robot_file),
    })
    assert result == {
        "artifact": payload["artifact_digest"],
        "specification_digest": payload["specification_digest"],
        "platform": "linux_amd64",
    }
    acquire = next(item for item in calls if item[0] == "acquire")
    assert acquire[1][1:] == (
        robot_file,
        "v18.19.5",
        payload["specification_digest"],
        payload["legacy_blueprint_key"],
    )
    assert acquire[2]["strict_identity"] is True
    assert acquire[2]["artifact_digest"] == payload["artifact_digest"]
    assert acquire[2]["expected_platform"] == "linux_amd64"
    assert acquire[2]["runtime_home"] == private_home
    assert [item[0] for item in calls if item[0] == "verify"] == ["verify"]
    assert str(archive) not in json.dumps(result)


@pytest.mark.parametrize(
    "descriptor,native",
    [("linux-x64", "linux_amd64"), ("win32-x64", "windows_amd64")],
)
def test_jat_platform_normalization_has_only_admitted_mappings(descriptor, native):
    assert runner.normalize_jat_platform(descriptor) == native


def test_jat_platform_normalization_rejects_unknown_values():
    with pytest.raises(runner.ManagedHaulerError, match="platform is invalid"):
        runner.normalize_jat_platform("darwin-arm64")


def _manifest(root: Path) -> Path:
    path = root / "content.yaml"
    path.write_text("apiVersion: content.hauler.cattle.io/v1\nkind: Images\n", encoding="utf-8")
    return path
