from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import room_store_hauler as component

REPOSITORY_ID = "a" * 64
SNAPSHOT_ID = "b" * 64
TREE_ID = "c" * 64
IMAGE_DIGEST = "sha256:" + "d" * 64
FILE_DIGEST = "sha256:" + "e" * 64


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

    def sync_image_txt(self, store, temp, sources, **kwargs):
        selected = Path(sources[0]).read_text(encoding="utf-8").splitlines()
        self.calls.append(("images", selected, kwargs))
        self.rows.extend(
            {"Reference": image, "Type": "image", "Digest": image.partition("@")[2] or IMAGE_DIGEST}
            for image in selected
        )

    def sync(self, store, temp, *manifests, **kwargs):
        self.calls.append(("manifests", tuple(Path(path).name for path in manifests), kwargs))
        for manifest in manifests:
            text = Path(manifest).read_text(encoding="utf-8")
            for line in text.splitlines():
                if "image:" in line:
                    image = line.split("image:", 1)[1].strip().strip("'\"")
                    self.rows.append({"Reference": image, "Type": "image", "Digest": IMAGE_DIGEST})

    def sync_files(self, store, temp, files, **kwargs):
        self.calls.append(("files", tuple(name for _path, name in files), kwargs))
        self.rows.extend({"Reference": name, "Type": "file", "Digest": FILE_DIGEST} for _path, name in files)

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
    prior = _prior(source_hash=digest)
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
        {"digest": IMAGE_DIGEST, "kind": "image"},
        {"digest": FILE_DIGEST, "kind": "file"},
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


def _manifest(root: Path) -> Path:
    path = root / "content.yaml"
    path.write_text("apiVersion: content.hauler.cattle.io/v1\nkind: Images\n", encoding="utf-8")
    return path
