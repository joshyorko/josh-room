from __future__ import annotations

import copy
import json
import struct
import sys

import pytest

from josh_room import homebrew_declared as declared
from josh_room import homebrew_prefix as base


def elf(
    *, needed=(), soname=None, interpreter=None, search=(), requires=None, provides=()
):
    """Synthetic native ELF containing only the loader tables under test."""
    requires = requires or {}
    data = bytearray(16384)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", data, 16, 3, 62, 1)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HHH", data, 52, 64, 56, 3 if interpreter else 2)
    strings = bytearray(b"\0")

    def string(value):
        offset = len(strings)
        strings.extend(value.encode() + b"\0")
        return offset

    entries = [(1, string(name)) for name in needed]
    if soname:
        entries.append((14, string(soname)))
    if search:
        entries.append((15, string(":".join(search))))
    cursor = 8192
    if requires:
        entries.extend([(0x6FFFFFFE, 0x400000 + cursor), (0x6FFFFFFF, len(requires))])
        for index, (library, versions) in enumerate(requires.items()):
            size = 16 + 16 * len(versions)
            struct.pack_into(
                "<HHIII",
                data,
                cursor,
                1,
                len(versions),
                string(library),
                16,
                size if index + 1 < len(requires) else 0,
            )
            for aux_index, version in enumerate(versions):
                struct.pack_into(
                    "<IHHII",
                    data,
                    cursor + 16 + aux_index * 16,
                    0,
                    0,
                    0,
                    string(version),
                    16 if aux_index + 1 < len(versions) else 0,
                )
            cursor += size
    if provides:
        entries.extend([(0x6FFFFFFC, 0x400000 + cursor), (0x6FFFFFFD, len(provides))])
        for index, version in enumerate(provides):
            struct.pack_into(
                "<HHHHIII",
                data,
                cursor,
                1,
                0,
                index + 1,
                1,
                0,
                20,
                28 if index + 1 < len(provides) else 0,
            )
            struct.pack_into("<II", data, cursor + 20, string(version), 0)
            cursor += 28
    entries.extend([(5, 0x400000 + 4096), (10, len(strings)), (0, 0)])
    for index, item in enumerate(entries):
        struct.pack_into("<qQ", data, 1024 + 16 * index, *item)
    data[4096 : 4096 + len(strings)] = strings
    headers = [
        (1, 5, 0, 0x400000, 0, len(data), len(data), 4096),
        (2, 6, 1024, 0x400400, 0, len(entries) * 16, len(entries) * 16, 8),
    ]
    if interpreter:
        value = interpreter.encode() + b"\0"
        data[512 : 512 + len(value)] = value
        headers.append((3, 4, 512, 0x400200, 0, len(value), len(value), 1))
    for index, header in enumerate(headers):
        struct.pack_into("<IIQQQQQQ", data, 64 + 56 * index, *header)
    return bytes(data)


def test_elf_reader_retains_required_and_provided_symbol_versions():
    value = declared.elf_metadata(
        elf(
            needed=["libc.so.6"],
            soname="libexample.so.1",
            requires={"libc.so.6": ["GLIBC_2.39"]},
            provides=["EXAMPLE_1.0"],
        )
    )
    assert value["requires"] == {"libc.so.6": ["GLIBC_2.39"]}
    assert value["provides"] == ["EXAMPLE_1.0"]
    assert value["soname"] == "libexample.so.1"


def test_receipt_graph_rejects_foreign_or_duplicate_dependencies():
    for dependencies in (
        [{"full_name": "foreign", "version": "1"}],
        [{"full_name": "oniguruma", "version": "1"}] * 2,
    ):
        with pytest.raises(base.PrefixError):
            declared._receipt(
                json.dumps(
                    {
                        "poured_from_bottle": True,
                        "source": {"tap": "homebrew/core"},
                        "runtime_dependencies": dependencies,
                    }
                ).encode()
            )


@pytest.fixture
def installed(tmp_path, monkeypatch):
    if sys.platform != "linux":
        pytest.skip("synthetic installed capture needs Linux no-follow fd support")
    root = tmp_path / "prefix"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(base, "PREFIX", str(root))
    monkeypatch.setattr(
        base,
        "native_platform",
        lambda: {"os": "linux", "arch": "x86_64", "ID": "debian", "VERSION_ID": "12"},
    )
    versions = {
        "jq": "1.8.2",
        "oniguruma": "6.9.10",
        "glibc": "2.42",
        "linux-headers@6.8": "6.8.12_1",
    }
    for name, version in versions.items():
        keg = root / "Cellar" / name / version
        (keg / "lib").mkdir(mode=0o700, parents=True)
        dependencies = (
            [{"full_name": "oniguruma", "version": "6.9.10", "pkg_version": "6.9.10"}]
            if name == "jq"
            else []
        )
        if name == "glibc":
            dependencies = [
                {
                    "full_name": "linux-headers@6.8",
                    "pkg_version": "6.8.12_1",
                    "version": "6.8.12",
                }
            ]
        (keg / "INSTALL_RECEIPT.json").write_text(
            json.dumps(
                {
                    "poured_from_bottle": True,
                    "source": {"tap": "homebrew/core"},
                    "runtime_dependencies": dependencies,
                }
            )
        )
        (root / "opt").mkdir(exist_ok=True)
        (root / "opt" / name).symlink_to("../Cellar/" + name + "/" + version)
    search = [str(root / "lib")]
    files = {
        "Cellar/jq/1.8.2/bin/jq": elf(
            needed=["libjq.so.1", "libonig.so.5", "libc.so.6"],
            interpreter=str(root / "lib/ld.so"),
            search=search,
            requires={"libc.so.6": ["GLIBC_2.39"]},
        ),
        "Cellar/jq/1.8.2/lib/libjq.so.1.0.4": elf(
            needed=["libm.so.6", "libonig.so.5", "libc.so.6", base.LOADER],
            soname="libjq.so.1",
            search=search,
        ),
        "Cellar/oniguruma/6.9.10/lib/libonig.so.5.5.0": elf(
            needed=["libc.so.6"], soname="libonig.so.5", search=search
        ),
        "Cellar/glibc/2.42/lib/libc.so.6": elf(
            needed=[base.LOADER], soname="libc.so.6", provides=["GLIBC_2.39"]
        ),
        "Cellar/glibc/2.42/lib/libm.so.6": elf(
            needed=["libc.so.6"], soname="libm.so.6"
        ),
        "Cellar/glibc/2.42/lib/" + base.LOADER: elf(soname=base.LOADER),
    }
    for name, value in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
        target.chmod(0o755)
    (root / "bin").mkdir()
    (root / "bin/jq").symlink_to("../Cellar/jq/1.8.2/bin/jq")
    (root / "lib").mkdir()
    for name, path in {
        "libjq.so.1": "Cellar/jq/1.8.2/lib/libjq.so.1",
        "libonig.so.5": "Cellar/oniguruma/6.9.10/lib/libonig.so.5",
        "libc.so.6": "Cellar/glibc/2.42/lib/libc.so.6",
        "libm.so.6": "Cellar/glibc/2.42/lib/libm.so.6",
        base.LOADER: "Cellar/glibc/2.42/lib/" + base.LOADER,
        "ld.so": "opt/glibc/lib/" + base.LOADER,
    }.items():
        (root / "lib" / name).symlink_to("../" + path)
    for name, target in {
        "Cellar/jq/1.8.2/lib/libjq.so.1": "libjq.so.1.0.4",
        "Cellar/oniguruma/6.9.10/lib/libonig.so.5": "libonig.so.5.5.0",
    }.items():
        (root / name).symlink_to(target)
    return root


def test_capture_freezes_only_declared_runtime_and_receipts(installed):
    (installed / "unrelated-user-file").write_text("must not be captured")
    manifest, data = declared.capture(installed, "jq")
    assert set(manifest["packages"]) == {
        "jq",
        "oniguruma",
        "glibc",
        "linux-headers@6.8",
    }
    assert len(data) == 10
    assert all(
        name.endswith("INSTALL_RECEIPT.json") or name in manifest["elf"]
        for name in data
    )
    assert "unrelated-user-file" not in data
    assert declared.validate_payload(manifest, data) is None
    assert manifest["elf"]["Cellar/jq/1.8.2/bin/jq"]["requires"] == {
        "libc.so.6": ["GLIBC_2.39"]
    }


@pytest.mark.parametrize(
    "change",
    [
        "dependency-version",
        "unsupported-needed",
        "unreachable",
        "version-missing",
        "outside-alias",
        "negative-size",
        "extra-field",
        "bad-package",
    ],
)
def test_manifest_refuses_unproven_or_unrelated_runtime(installed, change):
    manifest, _data = declared.capture(installed, "jq")
    bad = copy.deepcopy(manifest)
    if change == "dependency-version":
        bad["packages"]["jq"]["runtime_dependencies"][0]["version"] = "other"
    elif change == "unsupported-needed":
        bad["elf"]["Cellar/jq/1.8.2/bin/jq"]["needed"].append("libforeign.so.1")
    elif change == "unreachable":
        bad["elf"]["Cellar/jq/1.8.2/bin/jq"]["needed"] = ["libc.so.6"]
    elif change == "version-missing":
        bad["elf"]["Cellar/glibc/2.42/lib/libc.so.6"]["provides"] = ["GLIBC_2.36"]
    elif change == "outside-alias":
        bad["links"]["Cellar/jq/1.8.2/lib/libjq.so.1"] = "../../../../../outside"
    elif change == "negative-size":
        bad["files"]["Cellar/jq/1.8.2/bin/jq"]["bytes"] = -1
    elif change == "bad-package":
        bad["packages"]["oniguruma"] = "not-a-package"
    else:
        bad["other"] = True
    with pytest.raises(base.PrefixError):
        declared.validate_manifest(bad)


def test_capture_refuses_source_runtime_symbol_version_gap(installed):
    path = installed / "Cellar/glibc/2.42/lib/libc.so.6"
    path.write_bytes(
        elf(needed=[base.LOADER], soname="libc.so.6", provides=["GLIBC_2.36"])
    )
    with pytest.raises(base.PrefixError, match="symbol versions"):
        declared.capture(installed, "jq")


def test_capture_refuses_sensitive_material_in_receipt(installed):
    path = installed / "Cellar/jq/1.8.2/INSTALL_RECEIPT.json"
    value = json.loads(path.read_text())
    value["authorization"] = "Bearer synthetic-sensitive-value-1234567890"
    path.write_text(json.dumps(value))
    with pytest.raises(base.PrefixError, match="sensitive|material"):
        declared.capture(installed, "jq")


def test_validate_payload_uses_same_content_policy_as_capture(installed):
    manifest, data = declared.capture(installed, "jq")
    key = "Cellar/jq/1.8.2/INSTALL_RECEIPT.json"
    value = json.loads(data[key])
    value["authorization"] = "Bearer synthetic-sensitive-value-1234567890"
    data[key] = json.dumps(value).encode()
    digest = base._sha(data[key])
    manifest["files"][key].update(bytes=len(data[key]), sha256=digest)
    manifest["packages"]["jq"]["receipt_sha256"] = digest
    manifest["receipt_sha256"] = digest
    with pytest.raises(base.PrefixError, match="sensitive|material"):
        declared.validate_payload(manifest, data)


def test_unsupported_selected_package_does_not_traverse_source(tmp_path):
    with pytest.raises(base.PrefixError):
        declared.capture(tmp_path / "missing", "arbitrary-tool")


def test_v2_shared_archive_roundtrip_and_occupied_prefix_refusal(installed, tmp_path):
    manifest, data = declared.capture(installed, "jq")
    archive = base.prepare_archive((manifest, data), tmp_path / "jq.tar.zst")
    assert base.read_archive(archive) == (manifest, data)
    installed.rename(tmp_path / "synthetic-source")
    plan = base.plan_restore(archive, installed)
    result = base.restore(
        archive, installed, expected_manifest_sha256=plan["manifest_sha256"]
    )
    assert result["status"] == "restored" and result["format"] == declared.FORMAT
    for name, value in data.items():
        assert (installed / name).read_bytes() == value
    with pytest.raises(base.PrefixError, match="absent"):
        base.restore(
            archive, installed, expected_manifest_sha256=plan["manifest_sha256"]
        )
    assert all((installed / name).read_bytes() == value for name, value in data.items())


def test_v2_shared_restore_reports_swapped_stage_as_commit_uncertain(
    installed, tmp_path, monkeypatch
):
    import os

    manifest, data = declared.capture(installed, "jq")
    archive = base.prepare_archive((manifest, data), tmp_path / "jq.tar.zst")
    installed.rename(tmp_path / "synthetic-source")
    promote = base._promote

    def swap(parent, stage, destination):
        os.rename(stage, "displaced-stage", src_dir_fd=parent, dst_dir_fd=parent)
        os.mkdir(stage, dir_fd=parent)
        replacement = tmp_path / stage
        (replacement / "unrelated.txt").write_bytes(b"unrelated synthetic file")
        return promote(parent, stage, destination)

    monkeypatch.setattr(base, "_promote", swap)
    result = base.restore(
        archive, installed, expected_manifest_sha256=base.manifest_digest(manifest)
    )
    assert result["ok"] is False and result["status"] == "commit-uncertain"
    assert result["reconciliation_required"] is True
    assert (installed / "unrelated.txt").read_bytes() == b"unrelated synthetic file"
    assert (tmp_path / "displaced-stage/Cellar/jq/1.8.2/bin/jq").read_bytes() == data[
        "Cellar/jq/1.8.2/bin/jq"
    ]
