"""Bounded Linux libc-only Homebrew closure inside the existing recovery archive.

No installers, package scripts, or restored executables run here. The two OS
runtime files are captured bytes; their prefix links are explicitly normalized.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import struct
import sys
import tarfile
from pathlib import Path, PurePosixPath

FORMAT = "josh-room-homebrew-installed-v1"
PREFIX = "/home/linuxbrew/.linuxbrew"
ROOT = "homebrew-recovery"
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_ENTRIES = 5000
RUNTIME = ".josh-room-runtime"
LOADER = "ld-linux-x86-64.so.2"
QUALIFIED = {
    "Cellar/hello/2.12.3/bin/hello": "cbee33818fb29e6765db790c53c0dc20c59790991fc6d92226c255ecacff6e87",
    RUNTIME
    + "/"
    + LOADER: "02bcda52c1a5dfc236f94d9e5255b4a0e26347d8a372a5223b650e31f291ce3c",
    RUNTIME
    + "/libc.so.6": "6b4a45352fd0c540a9c7c718f35ce8c8e46a4e482f9d3885a910c32d1a0e1421",
}


class PrefixError(ValueError):
    """A closed, path-free installed-prefix admission failure."""


def _fail(message):
    raise PrefixError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def check_material(name, value=None):
    """Apply shared denied-path/content rules to package bytes without executing them."""
    from .material_security import Decision, MaterialClass, _path_class, scan_content

    if _path_class(Path(name)) is not None:
        _fail("sensitive material paths cannot enter an installed closure")
    if (
        value is not None
        and scan_content(
            (value,),
            adapter="homebrew-installed",
            material_class=MaterialClass.SESSION_ASSET,
            max_bytes=MAX_FILE,
        ).decision
        != Decision.ALLOW
    ):
        _fail("sensitive or unscannable material cannot enter an installed closure")


def native_platform():
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or os.open not in os.supports_dir_fd
    ):
        _fail(
            "installed Homebrew capture requires Linux x86_64 with no-follow dirfd support"
        )
    fields = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    value = {key: fields.get(key, "").strip('"') for key in ("ID", "VERSION_ID")}
    if not all(re.fullmatch(r"[a-zA-Z0-9._-]{1,32}", item) for item in value.values()):
        _fail("host OS identity is unsupported")
    if (value["ID"], value["VERSION_ID"]) != ("debian", "12"):
        _fail(
            "installed Homebrew runtime closure is currently qualified only on Debian 12 x86_64"
        )
    return {"os": "linux", "arch": "x86_64", **value}


def _directory(path):
    if (
        os.open not in os.supports_dir_fd
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        _fail("installed archive access requires qualified no-follow dirfd support")
    path = Path(path).absolute()
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in (".", ".."):
                _fail("prefix path is not canonical")
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read(path):
    parent = _directory(Path(path).parent)
    try:
        descriptor = os.open(
            Path(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or not 0 <= before.st_size <= MAX_FILE
            ):
                _fail("closure files must be bounded regular files without hardlinks")
            data = source.read(MAX_FILE + 1)
            if (
                len(data) != before.st_size
                or _signature(os.fstat(source.fileno())) != _signature(before)
                or _signature(
                    os.stat(Path(path).name, dir_fd=parent, follow_symlinks=False)
                )
                != _signature(before)
            ):
                _fail("installed Homebrew source changed during capture")
            return data, before
    finally:
        os.close(parent)


def _elf(data):
    try:
        if (
            data[:7] != b"\x7fELF\x02\x01\x01"
            or struct.unpack_from("<H", data, 18)[0] != 62
        ):
            _fail("only native Linux x86_64 ELF executables are supported")
        offset = struct.unpack_from("<Q", data, 32)[0]
        size, count = struct.unpack_from("<HH", data, 54)
        if size != 56 or count > 128:
            _fail("unsupported ELF program headers")
        headers = [
            struct.unpack_from("<IIQQQQQQ", data, offset + index * size)
            for index in range(count)
        ]
        interpreter = next(
            data[h[2] : h[2] + h[5]].rstrip(b"\0").decode()
            for h in headers
            if h[0] == 3
        )
        dynamic = next(h for h in headers if h[0] == 2)
        if dynamic[5] > MAX_FILE or dynamic[2] + dynamic[5] > len(data):
            _fail("unsupported ELF dynamic table")
        entries = [
            struct.unpack_from("<qQ", data, position)
            for position in range(dynamic[2], dynamic[2] + dynamic[5], 16)
        ]
        strings_address = next(value for key, value in entries if key == 5)
        load = next(
            h for h in headers if h[0] == 1 and h[3] <= strings_address < h[3] + h[5]
        )
        start = load[2] + strings_address - load[3]

        def string(index):
            end = data.index(b"\0", start + index)
            return data[start + index : end].decode()

        needed = [string(value) for key, value in entries if key == 1]
        paths = [string(value) for key, value in entries if key in (15, 29)]
        allowed_paths = {
            PREFIX + "/lib",
            PREFIX + "/Cellar/hello/2.12.3/lib",
            PREFIX + "/opt/gcc/lib/gcc/current",
        }
        if (
            needed != ["libc.so.6"]
            or interpreter != PREFIX + "/lib/ld.so"
            or not any(PREFIX + "/lib" in value.split(":") for value in paths)
            or any(
                part not in allowed_paths
                for value in paths
                for part in value.split(":")
            )
            or any(
                key in (0x7FFFFFFD, 0x7FFFFFFF, 0x6FFFFEFB, 0x6FFFFEFC)
                for key, _value in entries
            )
        ):
            _fail("formula is outside the supported libc-only exact-prefix ELF closure")
    except (ValueError, struct.error, StopIteration, IndexError, UnicodeError):
        _fail("formula is outside the supported libc-only exact-prefix ELF closure")


def _relative(value):
    if (
        not isinstance(value, str)
        or len(value) > 180
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or PurePosixPath(value).is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        _fail("installed manifest contains an unsafe relative path")
    return value


def _link(name, target):
    if (
        not isinstance(target, str)
        or not target
        or "\\" in target
        or "\x00" in target
        or target.startswith("/")
    ):
        _fail("installed manifest contains an unsafe link")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if resolved == ".." or resolved.startswith("../"):
        _fail("installed manifest link escapes the prefix")
    return _relative(resolved)


def capture(prefix, formula):
    """Read an explicitly selected immutable keg and its exact libc/loader bytes."""
    native = native_platform()
    if str(Path(prefix)) != PREFIX or not re.fullmatch(
        r"[a-z0-9][a-z0-9+.-]{0,63}", formula or ""
    ):
        _fail("select the standard exact Homebrew prefix and one supported formula")
    root = Path(prefix)
    root_fd = _directory(root)
    try:
        info = os.fstat(root_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            _fail("Homebrew prefix must be caller-owned and not group/world writable")
    finally:
        os.close(root_fd)
    opt = root / "opt" / formula
    if not opt.is_symlink():
        _fail("formula must have an installed opt link")
    keg = opt.resolve(strict=True)
    try:
        relative_keg = keg.relative_to(root).as_posix()
    except ValueError:
        _fail("formula opt link escapes the selected prefix")
    parts = relative_keg.split("/")
    if (
        len(parts) != 3
        or parts[:2] != ["Cellar", formula]
        or not re.fullmatch(r"[A-Za-z0-9._+-]{1,80}", parts[2])
    ):
        _fail("formula opt link does not identify a single installed keg")
    receipt, _ = _read(keg / "INSTALL_RECEIPT.json")
    try:
        provenance = json.loads(receipt)
        if (
            provenance["runtime_dependencies"]
            or provenance.get("poured_from_bottle") is not True
            or provenance["source"]["tap"] != "homebrew/core"
        ):
            _fail(
                "only official bottled formulae without formula runtime dependencies are supported"
            )
    except (ValueError, KeyError, TypeError):
        _fail("Homebrew installation receipt is unsupported")
    data = {}
    modes = {}
    links = {}
    directories = []
    signatures = {}

    def visit(directory):
        descriptor = _directory(directory)
        try:
            before = os.fstat(descriptor)
            names = []
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    names.append(entry.name)
                    if (
                        len(names) + len(data) + len(links) + len(directories)
                        > MAX_ENTRIES
                    ):
                        _fail("installed closure exceeds entry limits")
            for name in sorted(names):
                if name in {
                    ".ssh",
                    ".aws",
                    ".config",
                    ".netrc",
                    ".npmrc",
                    "auth.json",
                    "credentials",
                    "credentials.json",
                    "secrets.json",
                    "id_rsa",
                    "id_ed25519",
                }:
                    _fail(
                        "credential-like paths cannot be captured in an installed closure"
                    )
                path = directory / name
                relative = _relative(path.relative_to(root).as_posix())
                check_material(relative)
                metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if metadata.st_uid != os.getuid() or (
                    not stat.S_ISLNK(metadata.st_mode) and metadata.st_mode & 0o7022
                ):
                    _fail("installed keg has unsafe ownership or permissions")
                if stat.S_ISDIR(metadata.st_mode):
                    directories.append(relative)
                    visit(path)
                elif stat.S_ISLNK(metadata.st_mode):
                    _fail("keg-internal symlinks are outside the qualified v1 closure")
                elif stat.S_ISREG(metadata.st_mode):
                    value, info = _read(path)
                    check_material(relative, value)
                    if b"PRIVATE KEY-----" in value:
                        _fail(
                            "private key material cannot be captured in an installed closure"
                        )
                    data[relative] = value
                    modes[relative] = stat.S_IMODE(info.st_mode)
                    signatures[relative] = _signature(info)
                    if (
                        sum(map(len, data.values())) > MAX_BYTES
                        or len(data) + len(links) + len(directories) > MAX_ENTRIES
                    ):
                        _fail("installed closure exceeds capture limits")
                else:
                    _fail("installed keg contains an unsupported filesystem entry")
            if _signature(os.fstat(descriptor)) != _signature(before):
                _fail("installed keg changed during capture")
        finally:
            os.close(descriptor)

    visit(keg)
    executable = relative_keg + "/bin/" + formula
    if executable not in data:
        _fail("formula must expose a matching single native executable")
    _elf(data[executable])
    if any(
        name != executable and (value.startswith(b"\x7fELF") or modes[name] & 0o111)
        for name, value in data.items()
    ):
        _fail("additional executable files are outside this supported closure")
    for relative, expected in signatures.items():
        if _signature((root / relative).lstat()) != expected:
            _fail("installed source changed during capture")
    alias = root / "bin" / formula
    if not alias.is_symlink() or alias.resolve(strict=True) != keg / "bin" / formula:
        _fail("formula executable alias is unavailable or foreign")
    system_loader = Path("/lib64/" + LOADER).resolve(strict=True)
    system_libc = Path("/lib/x86_64-linux-gnu/libc.so.6").resolve(strict=True)
    loader_alias = root / "lib/ld.so"
    if (
        not loader_alias.is_symlink()
        or loader_alias.resolve(strict=True) != system_loader
    ):
        _fail("custom Homebrew loaders are outside the supported OS-libc closure")
    prefix_libc = root / "lib/libc.so.6"
    if prefix_libc.exists() and prefix_libc.resolve(strict=True) != system_libc:
        _fail("custom Homebrew libc is outside the supported closure")
    loader_bytes, loader_info = _read(system_loader)
    libc_bytes, libc_info = _read(system_libc)
    data[RUNTIME + "/" + LOADER] = loader_bytes
    data[RUNTIME + "/libc.so.6"] = libc_bytes
    modes.update(
        {
            RUNTIME + "/" + LOADER: stat.S_IMODE(loader_info.st_mode),
            RUNTIME + "/libc.so.6": stat.S_IMODE(libc_info.st_mode),
        }
    )
    links.update(
        {
            "opt/" + formula: "../" + relative_keg,
            "bin/" + formula: "../" + executable,
            "lib/ld.so": "../" + RUNTIME + "/" + LOADER,
            "lib/" + LOADER: "../" + RUNTIME + "/" + LOADER,
            "lib/libc.so.6": "../" + RUNTIME + "/libc.so.6",
        }
    )
    manifest = {
        "format": FORMAT,
        "prefix": PREFIX,
        "platform": native,
        "formula": formula,
        "version": parts[2],
        "receipt_sha256": _sha(receipt),
        "normalization": {
            "source_loader_link": os.readlink(loader_alias),
            "restored_loader_link": links["lib/ld.so"],
        },
        "directories": sorted({relative_keg, *directories}),
        "files": {
            name: {"sha256": _sha(value), "bytes": len(value), "mode": modes[name]}
            for name, value in sorted(data.items())
        },
        "links": dict(sorted(links.items())),
    }
    validate_manifest(manifest)
    return manifest, data


def validate_manifest(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {
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
        }
        or value["format"] != FORMAT
        or value["prefix"] != PREFIX
    ):
        _fail("unsupported installed Homebrew manifest")
    if (
        not isinstance(value["platform"], dict)
        or set(value["platform"]) != {"os", "arch", "ID", "VERSION_ID"}
        or value["platform"]["os"] != "linux"
        or value["platform"]["arch"] != "x86_64"
    ):
        _fail("unsupported installed Homebrew platform")
    if (
        not isinstance(value["formula"], str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,63}", value["formula"])
        or not isinstance(value["version"], str)
        or not re.fullmatch(r"[A-Za-z0-9._+-]{1,80}", value["version"])
    ):
        _fail("invalid installed formula identity")
    keg = "Cellar/" + value["formula"] + "/" + value["version"]
    files, links, dirs = value["files"], value["links"], value["directories"]
    if (
        not isinstance(files, dict)
        or not isinstance(links, dict)
        or not isinstance(dirs, list)
        or len(files) + len(links) + len(dirs) > MAX_ENTRIES
        or len(set(dirs)) != len(dirs)
    ):
        _fail("invalid installed closure inventory")
    allowed = lambda name: (
        name.startswith(keg + "/")
        or name in {RUNTIME + "/" + LOADER, RUNTIME + "/libc.so.6"}
    )
    if (
        not isinstance(value["receipt_sha256"], str)
        or not re.fullmatch("[0-9a-f]{64}", value["receipt_sha256"])
        or not isinstance(value["normalization"], dict)
        or set(value["normalization"]) != {"source_loader_link", "restored_loader_link"}
        or value["normalization"]["restored_loader_link"]
        != "../" + RUNTIME + "/" + LOADER
        or value["normalization"]["source_loader_link"]
        not in {
            "/lib64/" + LOADER,
            "/lib/x86_64-linux-gnu/" + LOADER,
            "/usr/lib/x86_64-linux-gnu/" + LOADER,
        }
    ):
        _fail("unsupported loader normalization provenance")
    if any(
        not isinstance(value["platform"][key], str)
        or not re.fullmatch("[A-Za-z0-9._-]{1,32}", value["platform"][key])
        for key in ("ID", "VERSION_ID")
    ):
        _fail("invalid OS platform identity")
    total = 0
    for name, record in files.items():
        _relative(name)
        check_material(name)
        if (
            not allowed(name)
            or not isinstance(record, dict)
            or set(record) != {"sha256", "bytes", "mode"}
            or not isinstance(record["sha256"], str)
            or not re.fullmatch("[0-9a-f]{64}", record["sha256"])
            or type(record["bytes"]) is not int
            or not 0 <= record["bytes"] <= MAX_FILE
            or type(record["mode"]) is not int
            or record["mode"] & ~0o755
        ):
            _fail("invalid installed file descriptor")
        total += record["bytes"]
    if total > MAX_BYTES or any(
        not (_relative(name) == keg or name.startswith(keg + "/")) for name in dirs
    ):
        _fail("installed closure exceeds its scope")
    required_links = {
        "opt/" + value["formula"]: "../" + keg,
        "bin/" + value["formula"]: "../" + keg + "/bin/" + value["formula"],
        "lib/ld.so": "../" + RUNTIME + "/" + LOADER,
        "lib/" + LOADER: "../" + RUNTIME + "/" + LOADER,
        "lib/libc.so.6": "../" + RUNTIME + "/libc.so.6",
    }
    if any(links.get(name) != target for name, target in required_links.items()) or any(
        name not in required_links for name in links
    ):
        _fail("invalid installed runtime linkage")
    all_names = set(files) | set(links) | set(dirs)
    if len(all_names) != len(files) + len(links) + len(dirs):
        _fail("installed manifest has path collisions")
    for name, target in links.items():
        _relative(name)
        resolved = _link(name, target)
        visited = {name}
        while resolved in links:
            if resolved in visited:
                _fail("installed manifest has a symlink cycle")
            visited.add(resolved)
            resolved = _link(resolved, links[resolved])
        if resolved not in all_names:
            _fail("installed link is dangling")
    for name in all_names:
        for parent in PurePosixPath(name).parents:
            if str(parent) in files or str(parent) in links:
                _fail("installed manifest has a non-directory parent")
    for required in [
        keg + "/bin/" + value["formula"],
        keg + "/INSTALL_RECEIPT.json",
        RUNTIME + "/" + LOADER,
        RUNTIME + "/libc.so.6",
    ]:
        if required not in files:
            _fail("installed runtime closure is incomplete")
    if (
        value["formula"] != "hello"
        or value["version"] != "2.12.3"
        or any(files[name]["sha256"] != digest for name, digest in QUALIFIED.items())
    ):
        _fail(
            "installed package or OS runtime bytes are outside the qualified Hello build"
        )
    if (
        any(
            files[name]["mode"] & 0o500 != 0o500
            for name in (
                keg + "/bin/hello",
                RUNTIME + "/" + LOADER,
            )
        )
        or not files[RUNTIME + "/libc.so.6"]["mode"] & 0o400
    ):
        _fail("qualified executable/runtime modes must permit owner execution/read")
    return value


def manifest_digest(manifest):
    return _sha(_json(validate_manifest(manifest)))


def prepare_archive(captured, output):
    import zstandard

    manifest, data = captured
    validate_manifest(manifest)
    for name, content in data.items():
        check_material(name, content)
    directory_names = {ROOT, ROOT + "/installed"}
    for name in [*data, *manifest["directories"]]:
        path = PurePosixPath(ROOT + "/installed/" + name)
        if name in manifest["directories"]:
            directory_names.add(str(path))
        directory_names.update(
            str(parent) for parent in path.parents if str(parent) != "."
        )
    with Path(output).open("xb") as raw:
        os.chmod(output, 0o600)
        with (
            zstandard.ZstdCompressor().stream_writer(raw, closefd=False) as compressed,
            tarfile.open(
                fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT
            ) as tar,
        ):
            for name in sorted(directory_names, key=lambda n: (n.count("/"), n)):
                entry = tarfile.TarInfo(name)
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o700
                tar.addfile(entry)
            for name, content in [
                ("Brewfile", ('brew "' + manifest["formula"] + '"\n').encode()),
                ("installed-prefix.json", _json(manifest)),
                *[("installed/" + name, value) for name, value in sorted(data.items())],
            ]:
                entry = tarfile.TarInfo(ROOT + "/" + name)
                entry.size = len(content)
                entry.mode = 0o600
                tar.addfile(entry, io.BytesIO(content))
    return Path(output)


def read_archive(archive):
    return _parse_archive(_archive_bytes(archive))


def _archive_bytes(archive):
    parent = _directory(Path(archive).absolute().parent)
    try:
        descriptor = os.open(
            Path(archive).name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
    finally:
        os.close(parent)
    with os.fdopen(descriptor, "rb") as raw:
        before = os.fstat(raw.fileno())
        limit = MAX_BYTES + 16 * 1024 * 1024
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            _fail("installed archive must be a bounded regular file")
        blob = raw.read(limit + 1)
        if len(blob) != before.st_size or _signature(before) != _signature(
            os.fstat(raw.fileno())
        ):
            _fail("installed archive changed during read")
        return blob


def _parse_archive(blob):
    import zstandard

    _preflight(blob)
    manifest = None
    data = {}
    seen = set()
    total = 0
    brewfile = None
    directories = set()
    with (
        io.BytesIO(blob) as raw,
        zstandard.ZstdDecompressor(max_window_size=128 * 1024 * 1024).stream_reader(
            raw
        ) as decoded,
        tarfile.open(fileobj=decoded, mode="r|") as tar,
    ):
        for entry in tar:
            if (
                entry.name in seen
                or len(seen) > MAX_ENTRIES + 100
                or not 0 <= entry.size <= MAX_FILE
                or not (entry.isdir() or entry.isfile())
            ):
                _fail("unsafe installed recovery archive")
            seen.add(entry.name)
            _relative(entry.name)
            if entry.isdir():
                directories.add(entry.name)
                if entry.name not in {
                    ROOT,
                    ROOT + "/installed",
                } and not entry.name.startswith(ROOT + "/installed/"):
                    _fail("unexpected installed archive directory")
                continue
            total += entry.size
            if total > MAX_BYTES + 1024 * 1024:
                _fail("installed recovery archive exceeds its limit")
            value = tar.extractfile(entry).read(MAX_FILE + 1)
            if len(value) != entry.size:
                _fail("incomplete installed recovery archive")
            if entry.name == ROOT + "/Brewfile":
                brewfile = value
            elif entry.name == ROOT + "/installed-prefix.json":
                if len(value) > 1024 * 1024:
                    _fail("installed manifest exceeds its limit")
                manifest = validate_manifest(
                    json.loads(value, object_pairs_hook=_unique_pairs)
                )
            elif entry.name.startswith(ROOT + "/installed/"):
                data[entry.name[len(ROOT + "/installed/") :]] = value
            else:
                _fail("unexpected installed recovery archive file")
    if (
        manifest is None
        or brewfile != ('brew "' + manifest["formula"] + '"\n').encode()
        or set(data) != set(manifest["files"])
    ):
        _fail("installed recovery archive is incomplete")
    expected_dirs = {ROOT, ROOT + "/installed"}
    for name in [*data, *manifest["directories"]]:
        path = PurePosixPath(ROOT + "/installed/" + name)
        if name in manifest["directories"]:
            expected_dirs.add(str(path))
        expected_dirs.update(
            str(parent) for parent in path.parents if str(parent) != "."
        )
    if directories != expected_dirs:
        _fail("installed recovery archive directory inventory mismatch")
    for name, value in data.items():
        check_material(name, value)
        record = manifest["files"][name]
        if len(value) != record["bytes"] or _sha(value) != record["sha256"]:
            _fail("installed file digest mismatch")
    if manifest["receipt_sha256"] != _sha(
        data[
            "Cellar/"
            + manifest["formula"]
            + "/"
            + manifest["version"]
            + "/INSTALL_RECEIPT.json"
        ]
    ):
        _fail("installed receipt provenance digest mismatch")
    _elf(
        data[
            "Cellar/"
            + manifest["formula"]
            + "/"
            + manifest["version"]
            + "/bin/"
            + manifest["formula"]
        ]
    )
    return manifest, data


def _identity(info):
    return info.st_dev, info.st_ino


def _bound(parent_fd, name, descriptor):
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return stat.S_ISDIR(named.st_mode) and _identity(named) == _identity(
            os.fstat(descriptor)
        )
    except OSError:
        return False


def _verify_restored(root_fd, manifest):
    """Verify exact topology and bytes through no-follow directory descriptors."""
    expected = (
        set(manifest["files"]) | set(manifest["links"]) | set(manifest["directories"])
    )
    for name in list(expected):
        expected.update(
            str(part) for part in PurePosixPath(name).parents if str(part) != "."
        )
    observed = set()

    def visit(descriptor, base=""):
        for leaf in os.listdir(descriptor):
            name = base + leaf
            if name not in expected:
                _fail("restored prefix contains unexpected material")
            observed.add(name)
            info = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            if name in manifest["files"]:
                record = manifest["files"][name]
                handle = os.open(
                    leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
                )
                with os.fdopen(handle, "rb") as source:
                    actual = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(actual.st_mode)
                        or actual.st_nlink != 1
                        or _identity(actual) != _identity(info)
                        or stat.S_IMODE(actual.st_mode) != record["mode"]
                        or actual.st_size != record["bytes"]
                    ):
                        _fail("restored prefix file identity changed")
                    value = source.read(MAX_FILE + 1)
                    if len(value) != record["bytes"] or _sha(value) != record["sha256"]:
                        _fail("restored prefix file content changed")
            elif name in manifest["links"]:
                if (
                    not stat.S_ISLNK(info.st_mode)
                    or os.readlink(leaf, dir_fd=descriptor) != manifest["links"][name]
                ):
                    _fail("restored prefix link changed")
            else:
                child = os.open(
                    leaf,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        _fail("restored prefix directory changed")
                    visit(child, name + "/")
                finally:
                    os.close(child)

    visit(root_fd)
    if observed != expected:
        _fail("restored prefix inventory is incomplete")


def _promote(parent_fd, stage_name, destination_name):
    rename = ctypes.CDLL(None, use_errno=True).renameat2
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    return rename(
        parent_fd, os.fsencode(stage_name), parent_fd, os.fsencode(destination_name), 1
    )


def _cleanup_stage(parent_fd, stage_name, stage_fd):
    # Delete through the held stage descriptor, never a replaceable parent name.
    # rmtree's final rmdir(".") cannot remove the root; remove that empty root
    # separately only while its parent name still identifies our directory.
    def onerror(function, path, error):
        if function is os.rmdir and path == ".":
            return
        raise error[1]

    shutil.rmtree(".", dir_fd=stage_fd, onerror=onerror)
    if _bound(parent_fd, stage_name, stage_fd):
        try:
            os.rmdir(stage_name, dir_fd=parent_fd)
        except OSError:
            pass


def _stage_parent(root_fd, name):
    descriptor = os.dup(root_fd)
    try:
        for part in PurePosixPath(name).parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def restore(archive, prefix, *, expected_manifest_sha256):
    from .cancellation import CLICancelled

    manifest, data = read_archive(archive)
    if (
        str(Path(prefix)) != manifest["prefix"]
        or manifest["platform"] != native_platform()
        or manifest_digest(manifest) != expected_manifest_sha256
    ):
        _fail("installed restore prefix, platform or reviewed manifest does not match")
    destination = Path(prefix)
    parent_fd = _directory(destination.parent)
    stage = None
    stage_fd = None
    stage_name = None
    created = False
    promoted = False
    promotion_attempted = False
    try:
        info = os.fstat(parent_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            _fail("restore parent must be caller-owned and private from other writers")
        try:
            os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail(
                "installed restore requires an absent prefix; no overwrite or merge is permitted"
            )
        import uuid

        stage_name = ".room-prefix-" + uuid.uuid4().hex
        os.mkdir(stage_name, mode=0o700, dir_fd=parent_fd)
        created = True
        stage_fd = os.open(
            stage_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        stage = Path("/proc/self/fd") / str(stage_fd)
        for name in manifest["directories"]:
            directory = _stage_parent(stage_fd, name + "/.directory")
            os.close(directory)
        for name, value in data.items():
            parent = _stage_parent(stage_fd, name)
            try:
                handle = os.open(
                    PurePosixPath(name).name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
            finally:
                os.close(parent)
            with os.fdopen(handle, "wb") as output:
                output.write(value)
                os.fchmod(output.fileno(), manifest["files"][name]["mode"])
                output.flush()
                os.fsync(output.fileno())
        for name, target in manifest["links"].items():
            parent = _stage_parent(stage_fd, name)
            try:
                os.symlink(target, PurePosixPath(name).name, dir_fd=parent)
            finally:
                os.close(parent)
        for directory, _directories, _files in os.walk(
            stage, topdown=False, followlinks=False
        ):
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        _verify_restored(stage_fd, manifest)
        fresh_parent = _directory(destination.parent)
        try:
            if _identity(os.fstat(fresh_parent)) != _identity(
                os.fstat(parent_fd)
            ) or not _bound(parent_fd, stage_name, stage_fd):
                _fail("restore parent or stage identity changed before promotion")
        finally:
            os.close(fresh_parent)
        promotion_attempted = True
        if _promote(parent_fd, stage_name, destination.name):
            promotion_attempted = False
            _fail("prefix admission changed; atomic no-clobber promotion refused")
        promoted = True
        stage = None
        created = False
        os.fsync(parent_fd)
        fresh_parent = _directory(destination.parent)
        try:
            if _identity(os.fstat(fresh_parent)) != _identity(
                os.fstat(parent_fd)
            ) or not _bound(parent_fd, destination.name, stage_fd):
                _fail("restored prefix binding changed during promotion")
            _verify_restored(stage_fd, manifest)
            if not _bound(parent_fd, destination.name, stage_fd):
                _fail("restored prefix binding changed after verification")
        finally:
            os.close(fresh_parent)
    except (OSError, PrefixError, KeyboardInterrupt, CLICancelled):
        if promoted or promotion_attempted:
            return {
                "ok": False,
                "status": "commit-uncertain",
                "reconciliation_required": True,
                "reason": "prefix promotion occurred but final identity/content could not be verified; inspect before retrying",
            }
        raise
    finally:
        if created and not promotion_attempted and stage_fd is not None:
            _cleanup_stage(parent_fd, stage_name, stage_fd)
        if stage_fd is not None:
            os.close(stage_fd)
        os.close(parent_fd)
    return {
        "ok": True,
        "status": "restored",
        "format": FORMAT,
        "manifest_sha256": manifest_digest(manifest),
        "file_count": len(data),
        "logical_bytes": sum(map(len, data.values())),
        "platform": manifest["platform"],
        "prefix": PREFIX,
        "restore_mode": "same-platform-exact-prefix-bundled-libc",
    }


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate installed manifest key")
        result[key] = value
    return result


def _preflight(blob):
    import zstandard

    scanned = 0
    count = 0
    with (
        io.BytesIO(blob) as raw,
        zstandard.ZstdDecompressor(max_window_size=128 * 1024 * 1024).stream_reader(
            raw
        ) as decoded,
    ):
        while True:
            header = decoded.read(512)
            scanned += len(header)
            if len(header) != 512:
                _fail("incomplete installed archive header")
            if header == b"\0" * 512:
                while block := decoded.read(1024 * 1024):
                    scanned += len(block)
                    if any(block) or scanned > MAX_BYTES + 8 * 1024 * 1024:
                        _fail("unexpected installed archive trailer")
                return
            try:
                entry = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
            except (ValueError, tarfile.HeaderError, UnicodeError):
                _fail("invalid installed archive header")
            count += 1
            if (
                count > MAX_ENTRIES + 100
                or entry.type
                not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                or not 0 <= entry.size <= MAX_FILE
                or (entry.isdir() and entry.size)
            ):
                _fail("unsupported installed archive member type or size")
            remaining = (entry.size + 511) // 512 * 512
            while remaining:
                block = decoded.read(min(remaining, 1024 * 1024))
                remaining -= len(block)
                scanned += len(block)
                if not block or scanned > MAX_BYTES + 8 * 1024 * 1024:
                    _fail("incomplete or oversized installed archive")


def capabilities():
    try:
        native = native_platform()
    except (OSError, PrefixError):
        return {
            "available": False,
            "reason": "requires qualified Debian 12 x86_64, owner-controlled exact prefix and tested Hello/runtime hashes",
        }
    return {
        "available": True,
        "format": FORMAT,
        "platform": native,
        "prefix": PREFIX,
        "reason": "conditional: tested Hello 2.12.3 and loader/libc hashes only; source admission checked in Preview",
    }


def plan_restore(archive, prefix):
    manifest, _data = read_archive(archive)
    if str(Path(prefix)) != PREFIX or manifest["platform"] != native_platform():
        _fail(
            "installed restore requires the captured OS/version/architecture and exact prefix"
        )
    parent = _directory(Path(prefix).parent)
    try:
        metadata = os.fstat(parent)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            _fail("restore parent is unsafe")
        try:
            os.stat(Path(prefix).name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("installed restore requires an absent prefix")
    finally:
        os.close(parent)
    return {
        "ok": True,
        "status": "ready",
        "manifest_sha256": manifest_digest(manifest),
        "prefix": PREFIX,
        "platform": manifest["platform"],
        "file_count": len(manifest["files"]),
        "logical_bytes": sum(record["bytes"] for record in manifest["files"].values()),
        "restore_mode": "same-platform-exact-prefix-bundled-libc",
    }


def export_archive(source, destination):
    blob = _archive_bytes(source)
    manifest, _data = _parse_archive(blob)
    destination = Path(destination)
    parent = _directory(destination.parent)
    digest = hashlib.sha256()
    copied = 0
    created = False
    try:
        descriptor = os.open(
            destination.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        created = True
        with os.fdopen(descriptor, "wb") as output, io.BytesIO(blob) as input_file:
            while block := input_file.read(1024 * 1024):
                copied += len(block)
                if copied > MAX_BYTES + 16 * 1024 * 1024:
                    _fail("installed component export exceeds its limit")
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(parent)
    except BaseException:
        if created:
            os.unlink(destination.name, dir_fd=parent)
        raise
    finally:
        os.close(parent)
    return {
        "ok": True,
        "status": "exported",
        "format": FORMAT,
        "manifest_sha256": manifest_digest(manifest),
        "archive_sha256": digest.hexdigest(),
        "archive_size": copied,
        "prefix": PREFIX,
        "platform": manifest["platform"],
    }
