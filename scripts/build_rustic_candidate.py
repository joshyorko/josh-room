"""Build a disposable qualification engine; never change installed runtime pins."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tarfile
import time
import tomllib
import urllib.request
from pathlib import Path, PurePosixPath

SOURCE = "6b96f976ff437a0528a4742358b172b32c3e3932"
CRATES = {
    "rustic_core-0.13.0": "ddab48a7f892b4943d9bffe7be44746c443982ae06ffcc3b2be55b31999b1725",
    "rustic_backend-0.7.0": "a0a52bffdb1a55a66f08cfc91f87ad928e502444e94ea88fce1c48c58ead909d",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepared_sources(destination: Path) -> dict[str, str]:
    """Bind every compiled or build-script input, including untracked files."""
    result = {}
    for name in ("source", *CRATES):
        tree = destination / name
        if tree.is_symlink() or not tree.is_dir():
            raise RuntimeError("candidate source root is not a directory")
        for path in sorted(tree.rglob("*")):
            relative = path.relative_to(tree)
            if relative.parts[0] == ".git":
                continue
            if path.is_symlink():
                raise RuntimeError("candidate source contains a symlink")
            if path.is_file():
                result[name + "/" + relative.as_posix()] = digest(path)
    return result


def verify_prepared_sources(destination: Path) -> None:
    source = destination / "source"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    manifest = destination / "prepared-source.json"
    if head != SOURCE or not manifest.is_file() or json.loads(manifest.read_text()) != prepared_sources(destination):
        raise RuntimeError("prepared candidate source changed; use a fresh destination")


def run(destination: Path, *, jobs: int = 2, verify_tests: bool = False) -> None:
    repository = Path(__file__).resolve().parents[1]
    destination.mkdir(parents=True, exist_ok=True)
    source = destination / "source"
    marker = destination / "candidate-inputs.json"
    patches = [
        ("source", "rustic-0.11.4-json-nodes.patch"),
        ("rustic_core-0.13.0", "rustic_core-0.13.0-windows-readonly.patch"),
        ("rustic_core-0.13.0", "rustic_core-0.13.0-verified-restore.patch"),
        ("rustic_backend-0.7.0", "rustic_backend-0.7.0-scoped-ca.patch"),
    ]
    inputs = {"upstream_source": SOURCE, "patch_sha256": {
        name: hashlib.sha256((repository / "docs/patches" / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for _, name in patches}}
    fresh = not marker.exists()
    if fresh:
        if not source.exists():
            subprocess.run(["git", "clone", "--depth", "1", "--branch", "v0.11.4",
                            "https://github.com/rustic-rs/rustic.git", str(source)], check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=source)
        if head != SOURCE or dirty:
            raise RuntimeError("candidate requires the exact clean disposable source checkout")
        for name, checksum in CRATES.items():
            package = name.rsplit("-", 1)[0]
            archive = destination / (name + ".crate")
            with urllib.request.urlopen(f"https://static.crates.io/crates/{package}/{name}.crate", timeout=60) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise RuntimeError("dependency source archive did not match upstream Cargo.lock")
            archive.write_bytes(raw)
            with tarfile.open(archive, "r:gz") as stream:
                members = stream.getmembers()
                if any(not (m.isfile() or m.isdir()) or PurePosixPath(m.name).is_absolute()
                       or ".." in PurePosixPath(m.name).parts or "\\" in m.name or ":" in m.name for m in members):
                    raise RuntimeError("unsafe verified dependency archive")
                stream.extractall(destination, filter="data")
        for target, name in patches:
            canonical = destination / name
            canonical.write_bytes((repository / "docs/patches" / name).read_bytes().replace(b"\r\n", b"\n"))
            subprocess.run(["git", "apply", "--check", "--unidiff-zero", str(canonical)], cwd=destination / target, check=True)
            subprocess.run(["git", "apply", "--unidiff-zero", str(canonical)], cwd=destination / target, check=True)
        original_lock = (source / "Cargo.lock").read_bytes()
        (destination / "original-Cargo.lock").write_bytes(original_lock)
        with (source / "Cargo.toml").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write('\n[patch.crates-io]\nrustic_core = { path = "../rustic_core-0.13.0" }\nrustic_backend = { path = "../rustic_backend-0.7.0" }\n')
        marker.write_text(json.dumps(inputs, indent=2) + "\n")
    elif json.loads(marker.read_text()) != inputs:
        raise RuntimeError("candidate inputs changed; use a fresh destination")
    else:
        verify_prepared_sources(destination)
    # Match upstream build.sh, which supplies git describe through PROJECT_VERSION.
    env = dict(os.environ, CARGO_TARGET_DIR=str(destination / "target"), CARGO_INCREMENTAL="0",
               PROJECT_VERSION="v0.11.4")
    log = destination / "build.log"
    started = time.monotonic()
    with log.open("wb") as stream:
        subprocess.run(["cargo", "metadata", "--format-version", "1"], cwd=source, env=env,
                       check=True, stdout=subprocess.DEVNULL, stderr=stream)
        before = tomllib.loads((destination / "original-Cargo.lock").read_text())
        after = tomllib.loads((source / "Cargo.lock").read_text())
        versions = lambda lock: {(p["name"], p["version"]) for p in lock["package"]}
        if versions(before) != versions(after):
            raise RuntimeError("candidate resolution changed dependency versions")
        for package in after["package"]:
            if package["name"] not in {"rustic_core", "rustic_backend"}:
                original = next(p for p in before["package"] if p["name"] == package["name"] and p["version"] == package["version"])
                if package.get("checksum") != original.get("checksum"):
                    raise RuntimeError("candidate resolution changed a dependency checksum")
        if fresh:
            (destination / "prepared-source.json").write_text(json.dumps(prepared_sources(destination), indent=2, sort_keys=True) + "\n")
        verify_prepared_sources(destination)
        command = ["cargo", "build", "--locked", "--release", "--features", "release", "--jobs", str(jobs)]
        subprocess.run(command, cwd=source, env=env, check=True, stdout=stream, stderr=subprocess.STDOUT)
    verify_prepared_sources(destination)
    unit_proofs = []
    if verify_tests:
        for package, test, count in (("rustic_core", "candidate_tree_auth", 2), ("rustic_backend", "scoped_ca_rejects", 1)):
            proof_log = destination / (package + "-tests.log")
            with proof_log.open("wb") as stream:
                subprocess.run(["cargo", "test", "--locked", "--release", "--lib", "-p", package,
                                "--jobs", str(jobs), test, "--", "--nocapture"], cwd=source, env=env,
                               check=True, stdout=stream, stderr=subprocess.STDOUT)
            if f"test result: ok. {count} passed; 0 failed" not in proof_log.read_text():
                raise RuntimeError("native unit proof did not execute the expected assertions")
            unit_proofs.append({"package": package, "filter": test, "passed": count, "result": "PASS", "log_sha256": digest(proof_log)})
        verify_prepared_sources(destination)
    binary = destination / "target/release" / ("rustic.exe" if os.name == "nt" else "rustic")
    result = {**inputs, "scope": "disposable patched source qualification; not a distributed or installed runtime",
        "josh_room_source": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip(),
        "josh_room_tracked_source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repository)),
        "compiler": subprocess.check_output(["rustc", "--version"], text=True).strip(),
        "cargo": subprocess.check_output(["cargo", "--version"], text=True).strip(),
        "platform": "win32-x64" if os.name == "nt" else "linux-x64",
        "build": "cargo build --locked --release --features release", "jobs": jobs,
        "project_version": "v0.11.4; matches upstream build.sh git-describe convention",
        "seconds": round(time.monotonic() - started, 3), "binary_sha256": digest(binary),
        "version": subprocess.check_output([str(binary), "version"], text=True).strip(),
        "original_lock_sha256": digest(destination / "original-Cargo.lock"),
        "candidate_lock_sha256": digest(source / "Cargo.lock"), "dependency_versions_unchanged": True,
        "prepared_source_manifest_sha256": digest(destination / "prepared-source.json"),
        "native_unit_proofs": unit_proofs,
        "patched_files": {f"{tree}/{name}": digest(destination / tree / name) for tree, name in (
            ("source", "src/commands/ls.rs"), ("rustic_core-0.13.0", "src/backend/ignore/mapper.rs"),
            ("rustic_core-0.13.0", "src/backend/local_destination.rs"),
            ("rustic_core-0.13.0", "src/blob/tree.rs"), ("rustic_core-0.13.0", "src/commands/restore.rs"),
            ("rustic_backend-0.7.0", "Cargo.toml"), ("rustic_backend-0.7.0", "src/opendal.rs"))}}
    (destination / "build-receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--verify-tests", action="store_true")
    args = parser.parse_args()
    run(args.destination.resolve(), jobs=args.jobs, verify_tests=args.verify_tests)
