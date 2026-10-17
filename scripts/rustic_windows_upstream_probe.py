"""Test an upstream-only Windows patch in disposable, verified source trees.

This does not build, install or select a replacement product binary. It tests
the exact core patch separately from the still-failing official engine gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

CORE_SHA256 = "ddab48a7f892b4943d9bffe7be44746c443982ae06ffcc3b2be55b31999b1725"
CORE_URL = "https://static.crates.io/crates/rustic_core/rustic_core-0.13.0.crate"


def run(output: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("this probe requires native Windows")
    source_root = Path(__file__).resolve().parents[1]
    patch = source_root / "docs/patches/rustic_core-0.13.0-windows-readonly.patch"
    result = {
        "scope": "upstream-only core unit proof; official product binary is unchanged",
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_root, text=True).strip(),
        "crate_sha256": CORE_SHA256,
        "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
        "platform": "win32-x64",
        "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip(),
        "product_windows_readonly": "FAIL; tested independently with official binary",
        "steps": {},
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    def persist():
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    persist()
    with tempfile.TemporaryDirectory(prefix="rustic-upstream-proof-") as name:
        root = Path(name)
        archive = root / "core.crate"
        with urllib.request.urlopen(CORE_URL, timeout=60) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if hashlib.sha256(raw).hexdigest() != CORE_SHA256:
            raise RuntimeError("core archive did not match the pinned Cargo.lock")
        archive.write_bytes(raw)
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if any(not (m.isfile() or m.isdir()) or PurePosixPath(m.name).is_absolute()
                   or ".." in PurePosixPath(m.name).parts or "\\" in m.name or ":" in m.name
                   for m in members):
                raise RuntimeError("unsafe pinned source archive")
            source.extractall(root, filter="data")
        baseline = root / "rustic_core-0.13.0"
        patched = root / "patched"
        shutil.copytree(baseline, patched)
        subprocess.run(["git", "apply", "--check", "--unidiff-zero", str(patch)], cwd=patched, check=True)
        subprocess.run(["git", "apply", "--unidiff-zero", str(patch)], cwd=patched, check=True)
        relative = Path("src/backend/ignore/mapper.rs")
        marker = "#[cfg(all(test, windows))]\nmod windows_readonly_tests"
        patched_text = (patched / relative).read_text(encoding="utf-8")
        module = marker + patched_text.split(marker, 1)[1]
        # Exactly the same new assertions against the unmodified implementation.
        with (baseline / relative).open("a", encoding="utf-8", newline="\n") as target:
            target.write("\n" + module)
        env = dict(os.environ, CARGO_TARGET_DIR=str(root / "target"), CARGO_HOME=str(root / "cargo-home"))
        for label, tree in (("baseline", baseline), ("patched", patched)):
            log = output.with_name(f"windows-upstream-{label}.log")
            command = ["cargo", "test", "--locked", "--manifest-path", str(tree / "Cargo.toml"),
                       "--lib", "windows_readonly", "--", "--nocapture"]
            with log.open("wb") as stream:
                completed = subprocess.run(command, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                           timeout=1500, check=False)
            text = log.read_text(encoding="utf-8", errors="replace")
            summaries = [line for line in text.splitlines() if line.startswith("test result:")]
            result["steps"][label] = {"exit_code": completed.returncode, "summary": summaries}
            persist()
            expected = "FAILED. 0 passed; 2 failed" if label == "baseline" else "ok. 2 passed; 0 failed"
            if not any(expected in summary for summary in summaries):
                raise RuntimeError(f"{label} did not produce its exact expected native assertions; inspect retained log")
        result["result"] = "PASS upstream unit RED/GREEN; product still blocked"
        persist()
        print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)
