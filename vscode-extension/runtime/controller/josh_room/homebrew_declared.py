"""Metadata-declared jq/oniguruma runtime closure for the recovery archive.

This is intentionally one qualified package family, not arbitrary prefix capture.
No brew command, package script, loader, or captured executable is run here.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import struct
from pathlib import Path, PurePosixPath

FORMAT = "josh-room-homebrew-installed-v2"
_SYSTEM = {"libc.so.6", "libm.so.6", "ld-linux-x86-64.so.2"}
_ROLES = {
    "jq": "selected-runtime",
    "oniguruma": "declared-runtime-library",
    "glibc": "loader-runtime-prerequisite",
    "linux-headers@6.8": "declared-kernel-build-metadata-only",
}
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _base():
    from . import homebrew_prefix

    return homebrew_prefix


def _fail(message):
    _base()._fail(message)


def _identity(name, version):
    if (
        name not in _ROLES
        or not isinstance(version, str)
        or not _NAME.fullmatch(version)
    ):
        _fail("installed dependency identity is outside the qualified jq family")
    return "Cellar/" + name + "/" + version


def _receipt(blob):
    try:
        value = json.loads(blob, object_pairs_hook=_base()._unique_pairs)
        if (
            not isinstance(value, dict)
            or value.get("poured_from_bottle") is not True
            or value["source"]["tap"] != "homebrew/core"
        ):
            _fail("installed dependency must declare an official core bottle receipt")
        dependencies = value["runtime_dependencies"]
        if not isinstance(dependencies, list) or len(dependencies) > 1:
            _fail("installed dependency graph is outside the qualified jq family")
        result = []
        for item in dependencies:
            if not isinstance(item, dict) or item.get("full_name") not in {
                "oniguruma",
                "linux-headers@6.8",
            }:
                _fail("installed dependency graph is outside the qualified jq family")
            version = item.get("pkg_version") or item.get("version")
            name = item["full_name"]
            _identity(name, version)
            result.append({"name": name, "version": version})
        return result
    except (ValueError, TypeError, KeyError):
        _fail("installed dependency receipt is unsupported")


def elf_metadata(blob):
    """Bounded native ELF reader; returns only loader-relevant strings."""
    try:
        if (
            len(blob) < 64
            or blob[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<H", blob, 18)[0] != 62
        ):
            _fail("runtime closure requires native Linux x86_64 ELF files")
        kind = struct.unpack_from("<H", blob, 16)[0]
        offset = struct.unpack_from("<Q", blob, 32)[0]
        size, count = struct.unpack_from("<HH", blob, 54)
        if (
            kind not in (2, 3)
            or size != 56
            or not 0 < count <= 128
            or offset + count * size > len(blob)
        ):
            _fail("runtime ELF headers are unsupported")
        headers = [
            struct.unpack_from("<IIQQQQQQ", blob, offset + i * size)
            for i in range(count)
        ]
        if any(h[2] + h[5] > len(blob) for h in headers):
            _fail("runtime ELF segment is outside its file")
        interpreters = [blob[h[2] : h[2] + h[5]] for h in headers if h[0] == 3]
        if len(interpreters) > 1 or any(
            not v.endswith(b"\0") or b"\0" in v[:-1] for v in interpreters
        ):
            _fail("runtime ELF interpreter is unsupported")
        dynamic = [h for h in headers if h[0] == 2]
        if len(dynamic) != 1 or dynamic[0][5] % 16:
            _fail("runtime ELF dynamic table is unsupported")
        h = dynamic[0]
        entries = []
        for position in range(h[2], h[2] + h[5], 16):
            key, value = struct.unpack_from("<qQ", blob, position)
            if key == 0:
                break
            entries.append((key, value))
        else:
            _fail("runtime ELF dynamic table has no terminator")
        # These tags load additional objects outside DT_NEEDED.
        if any(
            key in {0x7FFFFFFD, 0x7FFFFFFF, 0x6FFFFEFB, 0x6FFFFEFC}
            for key, _ in entries
        ):
            _fail("runtime ELF auxiliary, filter or audit loading is unsupported")
        string_addresses = [v for k, v in entries if k == 5]
        string_sizes = [v for k, v in entries if k == 10]
        if (
            len(string_addresses) != 1
            or len(string_sizes) != 1
            or not 0 < string_sizes[0] <= len(blob)
        ):
            _fail("runtime ELF string table is unsupported")
        address = string_addresses[0]
        loads = [
            h
            for h in headers
            if h[0] == 1
            and h[3] <= address
            and address + string_sizes[0] <= h[3] + h[5]
        ]
        if len(loads) != 1:
            _fail("runtime ELF string table is unmapped")
        start = loads[0][2] + address - loads[0][3]
        strings = blob[start : start + string_sizes[0]]

        def text(index):
            if not 0 <= index < len(strings):
                _fail("runtime ELF string offset is unsupported")
            return strings[index : strings.index(b"\0", index)].decode("ascii")

        needed = [text(v) for k, v in entries if k == 1]
        paths = [text(v) for k, v in entries if k in (15, 29)]
        sonames = [text(v) for k, v in entries if k == 14]
        if (
            len(paths) > 1
            or len(sonames) > 1
            or len(set(needed)) != len(needed)
            or len(needed) > 8
        ):
            _fail("runtime ELF dependency/search metadata is ambiguous")
        for name in [*needed, *sonames]:
            if not _NAME.fullmatch(name):
                _fail("runtime ELF dependency must be a bounded basename")

        def mapped(address, size):
            matches = [
                h
                for h in headers
                if h[0] == 1 and h[3] <= address and address + size <= h[3] + h[5]
            ]
            if len(matches) != 1:
                _fail("runtime ELF version metadata is unmapped")
            return matches[0][2] + address - matches[0][3]

        def table(tag, count_tag):
            locations = [v for k, v in entries if k == tag]
            counts = [v for k, v in entries if k == count_tag]
            if not locations and not counts:
                return None, 0
            if len(locations) != 1 or len(counts) != 1 or not 0 < counts[0] <= 256:
                _fail("runtime ELF version table is unsupported")
            return locations[0], counts[0]

        requires = {}
        address, count = table(0x6FFFFFFE, 0x6FFFFFFF)
        for index in range(count):
            version, aux_count, library, aux, following = struct.unpack_from(
                "<HHIII", blob, mapped(address, 16)
            )
            name = text(library)
            if (
                version != 1
                or not 0 < aux_count <= 128
                or name not in needed
                or name in requires
            ):
                _fail("runtime ELF required versions are unsupported")
            versions = []
            aux_address = address + aux
            for aux_index in range(aux_count):
                _hash, _flags, _other, string_index, next_aux = struct.unpack_from(
                    "<IHHII", blob, mapped(aux_address, 16)
                )
                versions.append(text(string_index))
                if (aux_index + 1 < aux_count) != bool(next_aux):
                    _fail("runtime ELF required version chain is incomplete")
                aux_address += next_aux
            requires[name] = sorted(versions)
            if (index + 1 < count) != bool(following):
                _fail("runtime ELF required version table is incomplete")
            address += following
        provides = []
        address, count = table(0x6FFFFFFC, 0x6FFFFFFD)
        for index in range(count):
            version, _flags, _index, aux_count, _hash, aux, following = (
                struct.unpack_from("<HHHHIII", blob, mapped(address, 20))
            )
            if version != 1 or not 0 < aux_count <= 128:
                _fail("runtime ELF provided versions are unsupported")
            string_index, _next_aux = struct.unpack_from(
                "<II", blob, mapped(address + aux, 8)
            )
            provides.append(text(string_index))
            if (index + 1 < count) != bool(following):
                _fail("runtime ELF provided version table is incomplete")
            address += following
        return {
            "needed": needed,
            "search": paths[0].split(":") if paths else [],
            "soname": sonames[0] if sonames else None,
            "interpreter": interpreters[0][:-1].decode("ascii")
            if interpreters
            else None,
            "requires": requires,
            "provides": sorted(provides),
        }
    except (ValueError, struct.error, IndexError, UnicodeError):
        _fail("runtime ELF metadata is unsupported")


def _safe_mode(info, *, executable=False, system=False):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_mode & 0o7022
        or info.st_uid != (0 if system else os.getuid())
    ):
        _fail("runtime files have unsupported ownership, mode or hardlinks")
    mode = stat.S_IMODE(info.st_mode)
    if not mode & 0o400 or (executable and mode & 0o500 != 0o500):
        _fail("runtime file must be owner-readable/executable")
    return mode


def capture(prefix, formula):
    base = _base()
    native = base.native_platform()
    if formula != "jq" or str(Path(prefix)) != base.PREFIX:
        _fail("declared runtime capture currently supports jq at the exact prefix only")
    root = Path(prefix)
    fd = base._directory(root)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            _fail("Homebrew prefix must be owner-controlled")
    finally:
        os.close(fd)
    packages, blobs, modes, signatures, links = {}, {}, {}, {}, {}
    source_aliases = {}

    def add(path, *, executable=False):
        value, info = base._read(path)
        mode = _safe_mode(info, executable=executable)
        name = path.relative_to(root).as_posix()
        base._relative(name)
        base.check_material(name, value)
        blobs[name], modes[name] = value, mode
        signatures[str(path)] = base._signature(info)
        if (
            len(blobs) + len(links) > base.MAX_ENTRIES
            or sum(map(len, blobs.values())) > base.MAX_BYTES
        ):
            _fail("installed runtime closure exceeds capture limits")
        return name

    def source_alias(path):
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            _fail("source runtime alias must be a caller-owned symlink")
        source_aliases[str(path)] = base._signature(info)
        return os.readlink(path)

    queue = [("jq", None), ("glibc", None)]
    while queue:
        name, required = queue.pop(0)
        if name in packages:
            if required and packages[name]["version"] != required:
                _fail("installed dependency versions conflict")
            continue
        opt = root / "opt" / name
        if not opt.is_symlink():
            _fail("declared dependency requires an installed opt link")
        source_alias(opt)
        keg = opt.resolve(strict=True)
        version = keg.name
        relative = _identity(name, version)
        if keg != root / relative or (required and version != required):
            _fail("installed dependency opt link/version disagrees with its receipt")
        for directory in (keg,) if name == "linux-headers@6.8" else (keg, keg / "lib"):
            fd = base._directory(directory)
            try:
                metadata = os.fstat(fd)
                if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                    _fail("installed dependency directories must be owner-controlled")
            finally:
                os.close(fd)
        receipt_name = add(keg / "INSTALL_RECEIPT.json")
        dependencies = _receipt(blobs[receipt_name])
        expected_names = {
            "jq": ["oniguruma"],
            "oniguruma": [],
            "glibc": ["linux-headers@6.8"],
            "linux-headers@6.8": [],
        }[name]
        if [d["name"] for d in dependencies] != expected_names:
            _fail("declared package graph is outside jq/oniguruma/glibc support")
        packages[name] = {
            "version": version,
            "keg": relative,
            "receipt_sha256": base._sha(blobs[receipt_name]),
            "runtime_dependencies": dependencies,
            "role": _ROLES[name],
        }
        if name != "linux-headers@6.8":
            links["opt/" + name] = "../" + relative
        queue.extend((d["name"], d["version"]) for d in dependencies)
    executable = root / packages["jq"]["keg"] / "bin/jq"
    entry = add(executable, executable=True)
    alias = root / "bin/jq"
    if not alias.is_symlink() or alias.resolve(strict=True) != executable:
        _fail("selected jq executable alias is foreign or unavailable")
    source_alias(alias)
    links["bin/jq"] = "../" + entry
    runtime_keg = root / packages["glibc"]["keg"]
    system_paths = {
        name: (runtime_keg / "lib" / name).resolve(strict=True) for name in _SYSTEM
    }
    loader_alias = root / "lib/ld.so"
    if (
        not loader_alias.is_symlink()
        or loader_alias.resolve(strict=True) != system_paths[base.LOADER]
    ):
        _fail("custom Homebrew loader is outside the qualified runtime closure")
    observed_loader_link = source_alias(loader_alias)
    runtime = {}
    for name, path in system_paths.items():
        if path.parent != runtime_keg / "lib":
            _fail("Homebrew runtime file escapes its recorded glibc keg")
        relative = add(path, executable=True)
        runtime[name] = relative
        links["lib/" + name] = "../" + relative
    links["lib/ld.so"] = "../" + runtime[base.LOADER]

    def source_library(path):
        """Keep only sibling-basename link chains within one declared keg/lib."""
        seen = set()
        while path.is_symlink():
            relative = path.relative_to(root).as_posix()
            if relative in seen or len(seen) >= 8:
                _fail("installed library aliases cycle or exceed their limit")
            seen.add(relative)
            target = source_alias(path)
            if not _NAME.fullmatch(target):
                _fail("installed library aliases must target sibling basenames")
            links[relative] = target
            path = path.parent / target
        canonical = path.resolve(strict=True)
        if canonical != path or not any(
            path.parent == root / p["keg"] / "lib" for p in packages.values()
        ):
            _fail("installed runtime library is outside its declared dependency")
        return add(path, executable=True)

    pending = [entry, *runtime.values()]
    processed = {}
    while pending:
        relative = pending.pop(0)
        if relative in processed:
            continue
        metadata = elf_metadata(blobs[relative])
        _validate_search(metadata, packages, system=relative in runtime.values())
        if relative == entry and metadata["interpreter"] != base.PREFIX + "/lib/ld.so":
            _fail("jq interpreter is outside the exact prefix")
        if relative != entry and metadata["interpreter"] not in (
            None,
            "/lib64/" + base.LOADER,
            base.PREFIX + "/lib/" + base.LOADER,
            base.PREFIX + "/lib/ld.so",
            base.PREFIX + "/" + packages["glibc"]["keg"] + "/lib/" + base.LOADER,
        ):
            _fail("runtime library interpreter is unsupported")
        processed[relative] = metadata
        for needed in metadata["needed"]:
            if needed in _SYSTEM:
                # Refuse a source prefix override: do not silently change the loader's input.
                for directory in metadata["search"] + [base.PREFIX + "/lib"]:
                    candidate = Path(directory) / needed
                    if (
                        candidate.exists()
                        and candidate.resolve(strict=True) != system_paths[needed]
                    ):
                        _fail(
                            "custom system-library override is outside the qualified closure"
                        )
                continue
            if needed not in {"libjq.so.1", "libonig.so.5"}:
                _fail("runtime dependency is outside the qualified jq library family")
            candidates = [
                Path(directory) / needed
                for directory in metadata["search"] + [base.PREFIX + "/lib"]
            ]
            candidate = next((p for p in candidates if p.exists()), None)
            if candidate is None:
                _fail("declared runtime library is missing from its source search path")
            real = candidate.resolve(strict=True)
            owner = "jq" if needed == "libjq.so.1" else "oniguruma"
            keglib = root / packages[owner]["keg"] / "lib"
            if real.parent != keglib:
                _fail(
                    "runtime library resolution disagrees with declared package ownership"
                )
            # Preserve the original source's keg-local SONAME chain, not a foreign public alias.
            source = keglib / needed
            if source.resolve(strict=True) != real:
                _fail("declared library SONAME alias is inconsistent")
            target = source_library(source)
            links["lib/" + needed] = "../" + source.relative_to(root).as_posix()
            pending.append(target)
    for path, signature in {**signatures, **source_aliases}.items():
        if base._signature(Path(path).lstat()) != signature:
            _fail("installed runtime source changed during capture")
    directories = set()
    for name in [*blobs, *links]:
        directories.update(str(p) for p in PurePosixPath(name).parents if str(p) != ".")
    manifest = {
        "format": FORMAT,
        "prefix": base.PREFIX,
        "platform": native,
        "formula": "jq",
        "version": packages["jq"]["version"],
        "receipt_sha256": packages["jq"]["receipt_sha256"],
        "normalization": {
            "source_loader_link": observed_loader_link,
            "restored_loader_link": links["lib/ld.so"],
        },
        "directories": sorted(directories),
        "files": {
            name: {"sha256": base._sha(value), "bytes": len(value), "mode": modes[name]}
            for name, value in sorted(blobs.items())
        },
        "links": dict(sorted(links.items())),
        "packages": packages,
        "runtime": runtime,
        "elf": processed,
    }
    validate_manifest(manifest)
    validate_payload(manifest, blobs)
    return manifest, blobs


def _validate_search(metadata, packages, *, system=False):
    base = _base()
    if not isinstance(metadata, dict) or set(metadata) != {
        "needed",
        "search",
        "soname",
        "interpreter",
        "requires",
        "provides",
    }:
        _fail("runtime ELF descriptor is unsupported")
    if (
        not isinstance(metadata["needed"], list)
        or len(metadata["needed"]) > 8
        or any(
            not isinstance(n, str) or not _NAME.fullmatch(n) for n in metadata["needed"]
        )
    ):
        _fail("runtime ELF dependency descriptor is unsupported")
    if len(set(metadata["needed"])) != len(metadata["needed"]):
        _fail("runtime ELF dependencies are duplicated")
    allowed = {base.PREFIX + "/lib", base.PREFIX + "/opt/gcc/lib/gcc/current"}
    allowed.update(base.PREFIX + "/" + p["keg"] + "/lib" for p in packages.values())
    allowed.update(
        base.PREFIX + "/opt/" + name + "/lib"
        for name in packages
        if name != "linux-headers@6.8"
    )
    if (
        not isinstance(metadata["search"], list)
        or len(metadata["search"]) > 8
        or any(not isinstance(p, str) or p not in allowed for p in metadata["search"])
    ):
        _fail("runtime ELF searches an unsupported location")
    if not isinstance(metadata["requires"], dict) or set(metadata["requires"]) - set(
        metadata["needed"]
    ):
        _fail("runtime ELF required version inventory is invalid")
    version_lists = [metadata["provides"], *metadata["requires"].values()]
    if any(
        not isinstance(items, list)
        or len(items) > 256
        or any(not isinstance(item, str) or not _NAME.fullmatch(item) for item in items)
        or len(set(items)) != len(items)
        for items in version_lists
    ):
        _fail("runtime ELF symbol version inventory is invalid")
    if system and metadata["search"]:
        _fail("system runtime search overrides are unsupported")
    for name in (metadata["soname"],):
        if name is not None and (
            not isinstance(name, str) or not _NAME.fullmatch(name)
        ):
            _fail("runtime ELF SONAME is invalid")
    if metadata["interpreter"] not in (
        None,
        base.PREFIX + "/lib/ld.so",
        "/lib64/" + base.LOADER,
        base.PREFIX + "/lib/" + base.LOADER,
        base.PREFIX + "/" + packages["glibc"]["keg"] + "/lib/" + base.LOADER,
    ):
        _fail("runtime ELF interpreter descriptor is invalid")


def validate_manifest(value):
    base = _base()
    fields = {
        "format",
        "prefix",
        "platform",
        "formula",
        "version",
        "receipt_sha256",
        "normalization",
        "directories",
        "files",
        "links",
        "packages",
        "runtime",
        "elf",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["format"] != FORMAT
        or value["prefix"] != base.PREFIX
        or value["formula"] != "jq"
    ):
        _fail("unsupported declared Homebrew manifest")
    if value["platform"] != {
        "os": "linux",
        "arch": "x86_64",
        "ID": "debian",
        "VERSION_ID": "12",
    }:
        _fail("declared Homebrew platform is unsupported")
    packages = value["packages"]
    if not isinstance(packages, dict) or set(packages) != set(_ROLES):
        _fail(
            "manifest must contain the jq library/runtime graph and explicit kernel-header metadata"
        )
    for name, package in packages.items():
        if (
            not isinstance(package, dict)
            or set(package)
            != {"version", "keg", "receipt_sha256", "runtime_dependencies", "role"}
            or package["keg"] != _identity(name, package["version"])
            or package["role"] != _ROLES[name]
            or not isinstance(package["receipt_sha256"], str)
            or not _HASH.fullmatch(package["receipt_sha256"])
        ):
            _fail("invalid declared package descriptor")
    for name, package in packages.items():
        dependency = {"jq": "oniguruma", "glibc": "linux-headers@6.8"}.get(name)
        expected = (
            [{"name": dependency, "version": packages[dependency]["version"]}]
            if dependency
            else []
        )
        if package["runtime_dependencies"] != expected:
            _fail("manifest dependency graph is unsupported or incomplete")
    if (
        value["version"] != packages["jq"]["version"]
        or value["receipt_sha256"] != packages["jq"]["receipt_sha256"]
    ):
        _fail("selected package identity disagrees with its dependency descriptor")
    runtime = value["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime) != _SYSTEM
        or any(
            not isinstance(path, str)
            or not path.startswith(packages["glibc"]["keg"] + "/lib/")
            or not _NAME.fullmatch(
                path.removeprefix(packages["glibc"]["keg"] + "/lib/")
            )
            for path in runtime.values()
        )
    ):
        _fail("runtime loader/library mapping is incomplete")
    normalization = value["normalization"]
    if (
        not isinstance(normalization, dict)
        or set(normalization) != {"source_loader_link", "restored_loader_link"}
        or normalization["restored_loader_link"] != "../" + runtime[base.LOADER]
        or normalization["source_loader_link"]
        not in {
            "../opt/glibc/lib/ld.so",
            "../opt/glibc/lib/" + base.LOADER,
            "../" + packages["glibc"]["keg"] + "/lib/" + base.LOADER,
            base.PREFIX + "/opt/glibc/lib/" + base.LOADER,
            base.PREFIX + "/opt/glibc/bin/ld.so",
            base.PREFIX + "/" + packages["glibc"]["keg"] + "/lib/" + base.LOADER,
        }
    ):
        _fail("runtime loader normalization is unsupported")
    files, links, directories, elf = (
        value[k] for k in ("files", "links", "directories", "elf")
    )
    if (
        not isinstance(files, dict)
        or not isinstance(links, dict)
        or not isinstance(directories, list)
        or not isinstance(elf, dict)
        or len(files) + len(links) + len(directories) > base.MAX_ENTRIES
    ):
        _fail("declared runtime inventory is unsupported")
    receipts = {p["keg"] + "/INSTALL_RECEIPT.json" for p in packages.values()}
    entry = packages["jq"]["keg"] + "/bin/jq"
    allowed_library = lambda name: any(
        name.startswith(p["keg"] + "/lib/")
        and "/" not in name[len(p["keg"] + "/lib/") :]
        for package_name, p in packages.items()
        if package_name != "linux-headers@6.8"
    )
    total = 0
    for name, record in files.items():
        base._relative(name)
        base.check_material(name)
        if name not in receipts | {entry} | set(
            runtime.values()
        ) and not allowed_library(name):
            _fail("file is outside the declared runtime scope")
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "bytes", "mode"}
            or not isinstance(record["sha256"], str)
            or not _HASH.fullmatch(record["sha256"])
            or type(record["bytes"]) is not int
            or not 0 <= record["bytes"] <= base.MAX_FILE
            or type(record["mode"]) is not int
            or record["mode"] & ~0o755
            or not record["mode"] & 0o400
        ):
            _fail("invalid declared runtime file descriptor")
        if name not in receipts and record["mode"] & 0o500 != 0o500:
            _fail("runtime ELF file must permit owner execution/read")
        total += record["bytes"]
    if (
        total > base.MAX_BYTES
        or not receipts | {entry} | set(runtime.values()) <= set(files)
        or set(elf) != set(files) - receipts
    ):
        _fail("runtime file or ELF inventory is incomplete")
    if any(
        files[p["keg"] + "/INSTALL_RECEIPT.json"]["sha256"] != p["receipt_sha256"]
        for p in packages.values()
    ):
        _fail("runtime receipt digests disagree")
    required = {
        "bin/jq": "../" + entry,
        "lib/ld.so": "../" + runtime[base.LOADER],
        **{
            "opt/" + name: "../" + p["keg"]
            for name, p in packages.items()
            if name != "linux-headers@6.8"
        },
        **{"lib/" + name: "../" + path for name, path in runtime.items()},
    }
    for name, target in required.items():
        if links.get(name) != target:
            _fail("runtime aliases are incomplete")
    for name, target in links.items():
        base._relative(name)
        if name in required:
            continue
        if allowed_library(name):
            if not isinstance(target, str) or not _NAME.fullmatch(target):
                _fail("library link must target one sibling basename")
        elif name in {"lib/libjq.so.1", "lib/libonig.so.5"}:
            owner = "jq" if name.endswith("libjq.so.1") else "oniguruma"
            if target != "../" + packages[owner]["keg"] + "/lib/" + name.removeprefix(
                "lib/"
            ):
                _fail("runtime SONAME alias has foreign ownership")
        else:
            _fail("link is outside the declared runtime scope")
    if any(not isinstance(name, str) for name in directories):
        _fail("declared runtime directory names are invalid")
    all_names = set(files) | set(links) | set(directories)
    if len(all_names) != len(files) + len(links) + len(directories):
        _fail("declared runtime paths collide")
    expected_dirs = set()
    for name in [*files, *links]:
        expected_dirs.update(
            str(p) for p in PurePosixPath(name).parents if str(p) != "."
        )
    if (
        any(not isinstance(d, str) for d in directories)
        or set(directories) != expected_dirs
    ):
        _fail("declared runtime directory inventory disagrees")
    for name in all_names:
        base._relative(name)
        base.check_material(name)
        if any(str(p) in files or str(p) in links for p in PurePosixPath(name).parents):
            _fail("declared runtime has a non-directory parent")
    for name in links:
        _resolve(name, files, links, directories)
    for name, metadata in elf.items():
        _validate_search(metadata, packages, system=name in runtime.values())
    if elf[entry]["interpreter"] != base.PREFIX + "/lib/ld.so":
        _fail("selected executable interpreter disagrees")
    _validate_graph(value)
    return value


def _resolve(name, files, links, directories):
    seen = set()
    while name in links:
        if name in seen or len(seen) >= 8:
            _fail("runtime alias graph cycles or is too deep")
        seen.add(name)
        name = posixpath.normpath(posixpath.join(posixpath.dirname(name), links[name]))
    if name not in set(files) | set(directories):
        _fail("runtime alias is dangling")
    return name


def _validate_graph(value):
    """Every ELF input must be reachable and every NEEDED resolves in captured bytes."""
    base = _base()
    files, links, elf = value["files"], value["links"], value["elf"]
    entry = value["packages"]["jq"]["keg"] + "/bin/jq"
    todo, reached = [entry, value["runtime"][base.LOADER]], set()
    while todo:
        name = todo.pop()
        if name in reached:
            continue
        reached.add(name)
        for needed in elf[name]["needed"]:
            if needed not in _SYSTEM | {"libjq.so.1", "libonig.so.5"}:
                _fail("ELF needs an unsupported runtime library")
            candidates = []
            for directory in elf[name]["search"] + [base.PREFIX + "/lib"]:
                candidate = directory.removeprefix(base.PREFIX + "/") + "/" + needed
                if candidate in files or candidate in links:
                    candidates.append(
                        _resolve(candidate, files, links, value["directories"])
                    )
            if not candidates or candidates[0] not in elf:
                _fail("ELF dependency cannot resolve in the captured runtime")
            target = candidates[0]
            if elf[target]["soname"] != needed:
                _fail("captured runtime SONAME disagrees with ELF dependency")
            if needed in _SYSTEM and target != value["runtime"][needed]:
                _fail("system runtime dependency is shadowed")
            if needed in {"libjq.so.1", "libonig.so.5"}:
                owner = "jq" if needed == "libjq.so.1" else "oniguruma"
                if not target.startswith(value["packages"][owner]["keg"] + "/lib/"):
                    _fail("runtime dependency has the wrong declared package owner")
            if not set(elf[name]["requires"].get(needed, [])) <= set(
                elf[target]["provides"]
            ):
                _fail("captured runtime does not provide required ELF symbol versions")
            todo.append(target)
    if reached != set(elf):
        _fail("captured ELF files are unrelated to the selected runtime")


def validate_payload(manifest, data):
    base = _base()
    validate_manifest(manifest)
    if set(data) != set(manifest["files"]):
        _fail("declared runtime payload inventory disagrees")
    for name, value in data.items():
        record = manifest["files"][name]
        if not isinstance(value, bytes):
            _fail("declared runtime payload must contain bytes")
        base.check_material(name, value)
        if (
            not isinstance(value, bytes)
            or len(value) != record["bytes"]
            or base._sha(value) != record["sha256"]
            or b"PRIVATE KEY-----" in value
        ):
            _fail("declared runtime payload digest or content disagrees")
        if name in manifest["elf"] and elf_metadata(value) != manifest["elf"][name]:
            _fail("declared runtime ELF metadata disagrees with captured bytes")
    for package in manifest["packages"].values():
        if (
            _receipt(data[package["keg"] + "/INSTALL_RECEIPT.json"])
            != package["runtime_dependencies"]
        ):
            _fail("declared dependencies disagree with captured Homebrew receipt")
