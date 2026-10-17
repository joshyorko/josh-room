"""Profile actual native authentication startup on disposable synthetic storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from josh_room.rustic_store import RusticStore


def run(binary: Path, output: Path, samples: int = 5) -> None:
    result = {
        "scope": "real native reads of one authenticated tree; no provider or user authority",
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "platform": platform.system() + "-" + platform.machine(),
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "samples": {},
    }
    with tempfile.TemporaryDirectory(prefix="rustic-startup-profile-") as name:
        root = Path(name)
        password = root / "password"
        password.write_text("synthetic-startup-profile")
        password.chmod(0o600)
        workspace = root / "workspace"
        workspace.mkdir()
        for i in range(10):
            folder = workspace / str(i)
            folder.mkdir()
            (folder / "file").write_bytes(b"x" * 1024)
        with RusticStore(repository=root / "repo", password_file=password,
                         cache_dir=root / "cache", executable=str(binary)) as store:
            store.initialize()
            summary = store.backup(workspace)
            tree = store.snapshot(summary.snapshot_id).tree_id
            for mode in ("password", "operation-scoped-masterkey"):
                env = store._environment()
                if mode != "password":
                    _, raw = store._capture(["cat", "masterkey"])
                    env["RUSTIC_KEY"] = raw.decode().strip()
                    env.pop("RUSTIC_PASSWORD_FILE", None)
                seconds = []
                for _ in range(samples):
                    started = time.perf_counter()
                    proc = subprocess.run([str(binary), "--no-progress", "cat", "tree-blob", tree],
                                          env=env, cwd=store._scratch.name, capture_output=True,
                                          check=True, timeout=30)
                    assert not proc.stderr and proc.stdout.endswith(b"\n")
                    assert hashlib.sha256(proc.stdout[:-1]).hexdigest() == tree
                    seconds.append(round(time.perf_counter() - started, 6))
                result["samples"][mode] = {"seconds": seconds, "median_seconds": statistics.median(seconds)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.rustic.resolve(strict=True), args.output)
