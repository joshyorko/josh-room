import copy
import hashlib
import struct
import sys
from pathlib import Path

import pytest

from josh_room import homebrew_prefix as brew


def executable():
    data = bytearray(1024)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", data, 16, 2, 62, 1)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HHH", data, 52, 64, 56, 3)
    interpreter = (brew.PREFIX + "/lib/ld.so").encode() + b"\0"
    data[300 : 300 + len(interpreter)] = interpreter
    strings = b"libc.so.6\0" + (brew.PREFIX + "/lib").encode() + b"\0"
    data[600 : 600 + len(strings)] = strings
    for offset, header in [
        (64, (1, 5, 0, 0x400000, 0x400000, 1024, 1024, 4096)),
        (
            120,
            (
                3,
                4,
                300,
                0x400000 + 300,
                0x400000 + 300,
                len(interpreter),
                len(interpreter),
                1,
            ),
        ),
        (176, (2, 6, 400, 0x400000 + 400, 0x400000 + 400, 64, 64, 8)),
    ]:
        struct.pack_into("<IIQQQQQQ", data, offset, *header)
    for index, pair in enumerate([(5, 0x400000 + 600), (1, 0), (15, 10), (0, 0)]):
        struct.pack_into("<qQ", data, 400 + index * 16, *pair)
    return bytes(data)


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setattr(brew, "PREFIX", str(tmp_path / "prefix"))
    native = {"os": "linux", "arch": "x86_64", "ID": "debian", "VERSION_ID": "12"}
    monkeypatch.setattr(brew, "native_platform", lambda: native)
    keg = "Cellar/hello/2.12.3"
    contents = {
        keg + "/bin/hello": executable(),
        keg + "/INSTALL_RECEIPT.json": b"synthetic receipt",
        brew.RUNTIME + "/" + brew.LOADER: b"synthetic loader",
        brew.RUNTIME + "/libc.so.6": b"synthetic libc",
    }
    links = {
        "opt/hello": "../" + keg,
        "bin/hello": "../" + keg + "/bin/hello",
        "lib/ld.so": "../" + brew.RUNTIME + "/" + brew.LOADER,
        "lib/" + brew.LOADER: "../" + brew.RUNTIME + "/" + brew.LOADER,
        "lib/libc.so.6": "../" + brew.RUNTIME + "/libc.so.6",
    }
    manifest = {
        "format": brew.FORMAT,
        "prefix": brew.PREFIX,
        "platform": native,
        "formula": "hello",
        "version": "2.12.3",
        "receipt_sha256": hashlib.sha256(b"synthetic receipt").hexdigest(),
        "normalization": {
            "source_loader_link": "/lib64/" + brew.LOADER,
            "restored_loader_link": links["lib/ld.so"],
        },
        "directories": [keg, keg + "/bin"],
        "files": {
            name: {
                "sha256": hashlib.sha256(value).hexdigest(),
                "bytes": len(value),
                "mode": 0o755
                if name.endswith(("/hello", "/" + brew.LOADER))
                else 0o644,
            }
            for name, value in contents.items()
        },
        "links": links,
    }
    monkeypatch.setattr(
        brew,
        "QUALIFIED",
        {name: manifest["files"][name]["sha256"] for name in brew.QUALIFIED},
    )
    return manifest, contents


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux dirfd/renameat2 restore only"
)
def test_archive_roundtrip_and_absent_prefix_promotion(tmp_path, capture):
    manifest, data = capture
    archive = brew.prepare_archive(capture, tmp_path / "brew.tar.zst")
    parsed, restored = brew.read_archive(archive)
    assert parsed == manifest and restored == data
    plan = brew.plan_restore(archive, Path(brew.PREFIX))
    result = brew.restore(
        archive, Path(brew.PREFIX), expected_manifest_sha256=plan["manifest_sha256"]
    )
    assert result["status"] == "restored"
    for name, value in data.items():
        assert (Path(brew.PREFIX) / name).read_bytes() == value
    assert (Path(brew.PREFIX) / "lib/ld.so").readlink().as_posix() == manifest["links"][
        "lib/ld.so"
    ]
    with pytest.raises(brew.PrefixError, match="absent"):
        brew.restore(
            archive, Path(brew.PREFIX), expected_manifest_sha256=plan["manifest_sha256"]
        )
    for name, value in data.items():
        assert (Path(brew.PREFIX) / name).read_bytes() == value


@pytest.mark.parametrize(
    "kind",
    [
        "outside",
        "cycle",
        "collision",
        "extra",
        "unknown",
        "mode",
        "negative",
        "dangling",
        "missing-runtime",
    ],
)
def test_closed_manifest_rejects_unsafe_closures(capture, kind):
    manifest, _ = capture
    bad = copy.deepcopy(manifest)
    if kind == "outside":
        bad["links"]["Cellar/hello/2.12.3/evil"] = "../../../../escape"
    elif kind == "cycle":
        bad["links"].update(
            {"Cellar/hello/2.12.3/a": "b", "Cellar/hello/2.12.3/b": "a"}
        )
    elif kind == "collision":
        bad["links"]["Cellar/hello/2.12.3/bin/hello"] = "../INSTALL_RECEIPT.json"
    elif kind == "extra":
        bad["files"][".aws/credentials"] = next(iter(bad["files"].values()))
    elif kind == "unknown":
        bad["new_component"] = "anything"
    elif kind == "mode":
        next(iter(bad["files"].values()))["mode"] = 0o4755
    elif kind == "negative":
        next(iter(bad["files"].values()))["bytes"] = -1
    elif kind == "dangling":
        bad["links"]["Cellar/hello/2.12.3/evil"] = "absent"
    else:
        bad["files"].pop(brew.RUNTIME + "/libc.so.6")
    with pytest.raises(brew.PrefixError):
        brew.validate_manifest(bad)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux archive access only")
def test_platform_prefix_and_review_digest_mismatch_never_write(
    tmp_path, capture, monkeypatch
):
    archive = brew.prepare_archive(capture, tmp_path / "brew.tar.zst")
    with pytest.raises(brew.PrefixError):
        brew.restore(archive, Path(brew.PREFIX), expected_manifest_sha256="f" * 64)
    assert not Path(brew.PREFIX).exists()
    with pytest.raises(brew.PrefixError):
        brew.restore(
            archive,
            tmp_path / "wrong",
            expected_manifest_sha256=brew.manifest_digest(capture[0]),
        )
    monkeypatch.setattr(
        brew,
        "native_platform",
        lambda: {**capture[0]["platform"], "VERSION_ID": "foreign"},
    )
    with pytest.raises(brew.PrefixError):
        brew.restore(
            archive,
            Path(brew.PREFIX),
            expected_manifest_sha256=brew.manifest_digest(capture[0]),
        )
    assert not Path(brew.PREFIX).exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux archive access only")
def test_payload_corruption_is_rejected(tmp_path, capture):
    manifest, data = capture
    data[next(iter(data))] = b"corrupted"
    archive = brew.prepare_archive((manifest, data), tmp_path / "brew.tar.zst")
    with pytest.raises(brew.PrefixError, match="digest"):
        brew.read_archive(archive)
    assert not Path(brew.PREFIX).exists()


def test_unqualified_capability_is_unavailable(monkeypatch):
    monkeypatch.setattr(brew, "native_platform", lambda: brew._fail("unsupported"))
    assert brew.capabilities()["available"] is False


def test_internal_link_with_parent_segments_is_rejected(capture):
    manifest, _data = capture
    manifest["links"]["Cellar/hello/2.12.3/alias"] = "bin"
    manifest["links"]["Cellar/hello/2.12.3/escape"] = "alias/../../../../outside"
    with pytest.raises(brew.PrefixError, match="linkage"):
        brew.validate_manifest(manifest)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux archive access only")
def test_export_copies_only_the_frozen_validated_archive(
    tmp_path, capture, monkeypatch
):
    archive = brew.prepare_archive(capture, tmp_path / "brew.tar.zst")
    expected = archive.read_bytes()
    parse = brew._parse_archive

    def replace_after_read(blob):
        result = parse(blob)
        archive.write_bytes(b"replaced")
        return result

    monkeypatch.setattr(brew, "_parse_archive", replace_after_read)
    output = tmp_path / "export.tar.zst"
    result = brew.export_archive(archive, output)
    assert output.read_bytes() == expected
    assert result["archive_sha256"] == hashlib.sha256(expected).hexdigest()


def test_preflight_rejects_negative_tar_size():
    import tarfile

    import zstandard

    entry = tarfile.TarInfo("negative")
    entry.size = -1024
    blob = zstandard.ZstdCompressor().compress(entry.tobuf(format=tarfile.GNU_FORMAT))
    with pytest.raises(brew.PrefixError, match="size"):
        brew._preflight(blob)


@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux dirfd/renameat2 restore only"
)
def test_regular_cancellation_removes_private_stage_and_never_promotes(
    tmp_path, capture, monkeypatch
):
    from josh_room.cancellation import CLICancelled

    archive = brew.prepare_archive(capture, tmp_path / "brew.tar.zst")

    def cancel(_descriptor):
        raise CLICancelled()

    monkeypatch.setattr(brew.os, "fsync", cancel)
    with pytest.raises(CLICancelled):
        brew.restore(
            archive,
            Path(brew.PREFIX),
            expected_manifest_sha256=brew.manifest_digest(capture[0]),
        )
    assert not Path(brew.PREFIX).exists()
    assert list(tmp_path.glob(".room-prefix-*")) == []


def test_offline_cli_invalid_archive_stays_json(tmp_path, capsys):
    from josh_room.cli import main

    archive = tmp_path / "invalid.tar.zst"
    archive.write_bytes(b"not a zstd archive")
    assert (
        main(
            [
                "homebrew",
                "plan-restore",
                "--archive",
                str(archive),
                "--prefix",
                brew.PREFIX,
                "--json",
            ]
        )
        == 1
    )
    import json

    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_qualified_executable_must_remain_executable(capture):
    manifest, _data = capture
    manifest["files"]["Cellar/hello/2.12.3/bin/hello"]["mode"] = 0o644
    with pytest.raises(brew.PrefixError, match="modes"):
        brew.validate_manifest(manifest)
