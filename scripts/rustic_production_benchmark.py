"""Benchmark real production adapters and RoomStoreOperations on synthetic files.

No cloud provider, host credentials or existing repository is used. Cold means
empty application metadata caches, not evicted OS page caches. Both engines run
sequentially on this host; local catalog/keyset callbacks are synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from josh_room import room_store_operations
from josh_room.private_paths import protect_private_directory
from josh_room.rustic_store import RusticStore
from josh_room.workspace_policy import load_capture_policy
from scripts.git_recovery_acceptance import operations
from scripts.rustic_benchmark_profile import CONTRACT, Profile, child_cpu


def identity(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare(source: Path, restored: Path) -> None:
    expected = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
    observed = {p.relative_to(restored).as_posix() for p in restored.rglob("*") if p.is_file()}
    assert expected == observed, "restore file set differs"
    for relative in expected:
        assert identity(source / relative) == identity(restored / relative), "restored bytes differ"


def run(root: Path, binaries: dict[str, Path], count: int, directories: int, output: Path, *, node_stream: bool = False, profile: bool = False) -> dict:
    # The same instrumentation may benchmark a different clean worktree. Bind
    # provenance to the imported production adapter, not the instrument's path.
    source_root = Path(inspect.getfile(RusticStore)).resolve().parents[2]
    result = {
        "source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source_root, text=True).strip(),
        "source_tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=source_root, text=True).strip(),
        "tracked_source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source_root)),
        "source_files": {name: identity(Path(__file__) if name == "scripts/rustic_production_benchmark.py" else source_root / name) for name in (
            "src/josh_room/rustic_store.py", "src/josh_room/restic_store.py",
            "src/josh_room/room_store_operations.py", "scripts/rustic_production_benchmark.py",
            "scripts/git_recovery_acceptance.py", "scripts/rustic_benchmark_profile.py")},
        "platform": platform.system() + "-" + platform.machine(),
        "python": platform.python_version(),
        "fixture": {"files": count, "directories": directories, "bytes_per_file": 1024,
                    "changed_files": max(1, count // 100),
                    "changed_directories": min(directories, max(1, count // 100)),
                    "change_distribution": "first one percent of file IDs, distributed across directories",
                    "same_size_restored_mtime": True},
        "cache_contract": "cold application caches; OS caches are not evicted; sequential engine runs",
        "storage": "disposable local format-2 repositories; synthetic catalog/keyset callbacks",
        "engines": {},
        "rustic_metadata_format": "nodes-v1" if node_stream else "tree-blobs",
        "profiling_contract": CONTRACT if profile else "not enabled",
    }

    def persist():
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")

    persist()
    def run_engine(engine: str, binary: Path) -> None:
        engine_root = root / engine
        engine_root.mkdir()
        workspace = engine_root / "workspace"
        workspace.mkdir()
        for directory in range(directories):
            (workspace / f"d{directory:04d}").mkdir()
        for index in range(count):
            data = hashlib.sha256(f"synthetic-file-{index:06d}".encode()).digest() * 32
            (workspace / f"d{index % directories:04d}/f{index:06d}.bin").write_bytes(data)
        observer = Profile(profile).enter()
        op = operations(engine_root, workspace, engine=engine, executable=str(binary))
        metrics = {"binary_sha256": identity(binary), "seconds": {}, "command_counts": {}, "checks": {},
                   "command_profiles": {}, "corpus_sha256": {}, "phase_profiles": {}}
        result["engines"][engine] = metrics
        def corpus_identity():
            digest = hashlib.sha256()
            for file in sorted(workspace.rglob("*.bin")):
                digest.update(file.relative_to(workspace).as_posix().encode() + b"\x00")
                digest.update(hashlib.sha256(file.read_bytes()).digest())
            return digest.hexdigest()

        metrics["corpus_sha256"]["initial"] = corpus_identity()
        command_count = 0
        command_profiles = []
        detailed_native = []
        original_factory = op.store_factory

        def factory(**kwargs):
            if engine == "rustic" and node_stream:
                kwargs["metadata_format"] = "nodes-v1"
            store = original_factory(**kwargs)
            observer.store(store)

            def spawn(*args, **kwargs):
                nonlocal command_count
                command_count += 1
                started = time.perf_counter()
                cpu_before = child_cpu() if profile else None
                process = subprocess.Popen(*args, **kwargs)
                spawn_seconds = time.perf_counter() - started
                observer.native_start(process.pid)
                argv = args[0]
                label = "other"
                for verb in ("cat", "version", "init", "backup", "snapshots", "ls", "restore", "check"):
                    if verb in argv[1:]:
                        label = verb
                        if verb == "cat":
                            label += ":" + argv[argv.index(verb) + 1]
                        break
                wait = process.wait
                recorded = False

                def profiled_wait(*a, **k):
                    nonlocal recorded
                    try:
                        return wait(*a, **k)
                    finally:
                        if not recorded and process.poll() is not None:
                            # Never retain argv, environments or native output.
                            native_seconds = time.perf_counter() - started
                            command_profiles.append((label, native_seconds))
                            detailed_native.append((label, observer.native_finish(process.pid, spawn_seconds=spawn_seconds, native_seconds=native_seconds, cpu_before=cpu_before)))
                            recorded = True

                process.wait = profiled_wait
                return process

            store._process_factory = spawn
            return store

        op.store_factory = factory

        def timed(label, action):
            nonlocal command_count
            before_count = command_count
            before_profile = len(command_profiles)
            started = time.perf_counter()
            python_cpu = time.process_time()
            before_native = len(detailed_native)
            observer.phase = label
            value = action()
            phase_cpu = time.process_time() - python_cpu
            metrics["seconds"][label] = round(time.perf_counter() - started, 6)
            metrics["command_counts"][label] = command_count - before_count
            observed = observer.result(label, wall=metrics["seconds"][label], python_cpu=phase_cpu, native=detailed_native[before_native:])
            if observed is not None: metrics["phase_profiles"][label] = observed
            groups = {}
            for verb, seconds in command_profiles[before_profile:]:
                bucket = groups.setdefault(verb, {"count": 0, "seconds": 0.0})
                bucket["count"] += 1
                bucket["seconds"] += seconds
            metrics["command_profiles"][label] = {
                verb: {"count": values["count"], "seconds": round(values["seconds"], 6)}
                for verb, values in groups.items()
            }
            persist()
            print(f"{engine} {label}: {metrics['seconds'][label]:.3f}s", flush=True)
            return value

        policy = load_capture_policy(workspace)
        cold_scan = timed("metadata_scan_first", lambda: room_store_operations._scan_workspace(workspace, policy))
        warm_scan = timed("metadata_scan_repeat", lambda: room_store_operations._scan_workspace(workspace, policy))
        assert cold_scan.signature == warm_scan.signature
        assert len(cold_scan.paths) == count + directories
        initial = timed("initial_save", op.save)
        assert initial.status == "saved"
        if os.name != "nt":
            _keyset, password, scan_store = op._open_store()
            try:
                with scan_store as opened:
                    opened.open_existing()
                    opened.snapshot(initial.snapshot_id)
                    scanned = timed("adapter_warm_scan", lambda: opened.backup(workspace, parent=initial.snapshot_id))
                    assert scanned.snapshot_id is None
            finally:
                password.unlink(missing_ok=True)
        noop = timed("warm_unchanged_save", op.save)
        assert noop.status == "already-saved" and noop.data_added_bytes == 0
        for index in range(max(1, count // 100)):
            path = workspace / f"d{index % directories:04d}/f{index:06d}.bin"
            before = path.stat()
            path.write_bytes(hashlib.sha256(f"synthetic-edit-{index:06d}".encode()).digest() * 32)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        metrics["corpus_sha256"]["changed"] = corpus_identity()
        changed = timed("changed_save", op.save)
        assert changed.status == "saved" and changed.snapshot_id != initial.snapshot_id
        assert timed("changed_warm_unchanged_save", op.save).status == "already-saved"
        body = changed.descriptor.to_dict()["workspace"]
        _keyset, password, _unused = op._open_store()
        cold_cache = engine_root / "cold-cache"
        cold_cache.mkdir(mode=0o700)
        protect_private_directory(cold_cache)
        store = factory(repository=op.repository, password_file=password, cache_dir=cold_cache)
        try:
            with store as opened:
                opened.open_existing()
                inventory = timed("inventory_cold", lambda: list(opened.entries(body["snapshot_id"], expected_tree_id=body["tree_id"])))
                warm = timed("inventory_warm", lambda: list(opened.entries(body["snapshot_id"], expected_tree_id=body["tree_id"])))
                assert inventory == warm and len(inventory) in (count + directories, count + directories + 1)
                if engine == "rustic" and not node_stream:
                    shutil.rmtree(cold_cache / "rustic-inventory-v1")
                    trees = timed("inventory_warm_tree_blobs", lambda: list(opened.entries(body["snapshot_id"], expected_tree_id=body["tree_id"])))
                    assert inventory == trees
                if profile and engine == "rustic":
                    # A separate native-cache context keeps calibration out of
                    # the decisive cold/warm inventory and Save measurements.
                    probe_cache = engine_root / "profile-probe-cache"
                    probe_cache.mkdir(mode=0o700)
                    protect_private_directory(probe_cache)
                    with factory(repository=op.repository, password_file=password, cache_dir=probe_cache) as probe:
                        timed("profile_repository_open", probe.open_existing)
                        timed("profile_password_config_first", probe._read_repository_info)
                        timed("profile_password_config_repeat", probe._read_repository_info)
                        timed("profile_authentication_once", probe._prime_masterkey)
                        timed("profile_key_config_first", probe._read_repository_info)
                        timed("profile_key_config_repeat", probe._read_repository_info)
                        def root_blob():
                            _, raw = probe._capture(["cat", "tree-blob", body["tree_id"]])
                            assert raw.endswith(b"\n") and hashlib.sha256(raw[:-1]).hexdigest() == body["tree_id"]
                        timed("profile_index_and_root_cold", root_blob)
                        timed("profile_index_and_root_warm", root_blob)
                timed("full_data_check", lambda: opened.check(read_data=True))
                snapshots = timed("snapshot_inventory", opened.snapshots)
                assert len(snapshots) == 2
        finally:
            password.unlink(missing_ok=True)
        restored = engine_root / "restored"
        timed("staged_restore", lambda: op.restore(changed.descriptor, restored, write_restore_marker=lambda *_: None))
        timed("complete_restore_comparison", lambda: compare(workspace, restored))
        metrics["checks"] = {"all_restored_bytes": "PASS", "same_size_restored_mtime_edit": "PASS",
                             "no_op_zero_bytes_and_two_snapshots": "PASS", "inventory_equal": "PASS",
                             "full_data_check": "PASS"}
        metrics["data_added"] = {"initial": initial.data_added_bytes, "changed": changed.data_added_bytes}
        persist()
        observer.close()
    for engine, binary in binaries.items():
        run_engine(engine, binary)
    for state in ("initial", "changed"):
        assert len({metrics["corpus_sha256"][state] for metrics in result["engines"].values()}) == 1
    result["status"] = "PASS"
    persist()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restic", type=Path, required=True)
    parser.add_argument("--rustic", type=Path, required=True)
    parser.add_argument("--files", type=int, default=100000)
    parser.add_argument("--directories", type=int, default=250)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", action="store_true", help="observe actual phases, native CPU, spawn cost and sampled RSS")
    parser.add_argument("--rustic-node-stream", action="store_true", help="disposable patched engine only")
    args = parser.parse_args()
    if args.files < 1 or not 1 <= args.directories <= args.files:
        parser.error("require files >= directories >= 1")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rustic-production-benchmark-") as name:
        run(Path(name), {"restic": args.restic.resolve(strict=True), "rustic": args.rustic.resolve(strict=True)},
            args.files, args.directories, args.output.resolve(), node_stream=args.rustic_node_stream, profile=args.profile)


if __name__ == "__main__":
    main()
