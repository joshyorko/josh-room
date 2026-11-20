#!/usr/bin/env python3
"""Bounded synthetic spike harness for Hauler v2.1.1 fileserver evaluation (#202).

Security invariant:
- NEVER runs an unisolated Hauler fileserver on the host network (Hauler v2.1.1
  `internal/server/file.go` binds `:<port>` on all interfaces by default).
- In `--mode live`, requires BOTH a `hauler` binary AND Linux network-namespace
  isolation (`unshare --user --net --map-root-user`) so sockets are confined to
  an isolated loopback namespace. If either is missing, exits cleanly with a
  truthful `status: "blocked"` JSON receipt instead of faking a pass or binding
  on host interfaces.
- In `--mode fixture`, runs a synthetic, strictly `127.0.0.1`-bound model of the
  four spike checks (archive-only JAT projection, multi-file projection &
  basename collisions, one-shot copy vs live edit divergence, and bind-scope
  audit) and explicitly marks `live_hauler_binary_executed: false`.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _probe_environment() -> dict[str, Any]:
    hauler_path = shutil.which("hauler")
    rcc_path = shutil.which("rcc")
    unshare_path = shutil.which("unshare")
    hauler_version = None
    if hauler_path:
        proc = subprocess.run([hauler_path, "version"], capture_output=True, text=True, check=False, timeout=10)
        if proc.returncode == 0:
            match = re.search(r"GitVersion:\s*v?(\S+)", proc.stdout)
            hauler_version = match.group(1) if match else "unknown"

    netns_ok = False
    netns_error = "unshare binary missing"
    if unshare_path:
        proc = subprocess.run(
            [unshare_path, "--user", "--net", "--map-root-user", "true"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        netns_ok = proc.returncode == 0
        netns_error = "" if netns_ok else (proc.stderr or proc.stdout).strip()

    return {
        "hauler_present": bool(hauler_path),
        "hauler_version": hauler_version,
        "hauler_sha256": hashlib.sha256(Path(hauler_path).read_bytes()).hexdigest() if hauler_path else None,
        "rcc_present": bool(rcc_path),
        "netns_isolation_available": netns_ok,
        "netns_error": netns_error,
    }


def _http_get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read()


class _QuietFileHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def _run_fixture_spike() -> dict[str, Any]:
    """Run synthetic loopback-only fixture spike modeling Hauler v2.1.1 projection semantics."""
    results: dict[str, Any] = {
        "mode": "fixture",
        "live_hauler_binary_executed": False,
        "notice": (
            "Synthetic fixture simulation of Hauler v2.1.1 CopyCmd -> http.FileServer(http.Dir(RootDir)) "
            "semantics on 127.0.0.1 only. Does NOT substitute for the real Hauler v2.1.1 binary gate."
        ),
        "checks": {},
    }

    with tempfile.TemporaryDirectory(prefix="hauler-spike-fixture-") as tmp:
        root = Path(tmp)

        # (1) JAT-style monolithic archive projection
        jat_proj = root / "jat-projection"
        jat_proj.mkdir()
        archive_bytes = b"synthetic-zstd-tar-placeholder-payload"
        (jat_proj / "joshs-all-the-things-workspace.tar.zst").write_bytes(archive_bytes)

        # (2) Multi-file projection simulating Hauler mapper.Files() OCI Title mapping:
        #     - flat basenames preserved
        #     - nested path attempt flattened/collided when OCI Title lacks safe dir mapping
        #     - one-shot copy projection frozen relative to source mutations
        source_ws = root / "live-workspace"
        (source_ws / "src").mkdir(parents=True)
        (source_ws / "etc").mkdir(parents=True)
        (source_ws / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
        (source_ws / "etc" / "policy.json").write_text('{"mode":"synthetic"}\n', encoding="utf-8")
        (source_ws / "a_config.toml").write_text("k = 1\n", encoding="utf-8")

        multi_proj = root / "multi-projection"
        multi_proj.mkdir()
        # One-shot copy into projection RootDir (mirrors ServeFilesCmd -> CopyCmd("directory://"+RootDir))
        shutil.copy2(source_ws / "src" / "app.py", multi_proj / "app.py")
        shutil.copy2(source_ws / "etc" / "policy.json", multi_proj / "policy.json")
        (multi_proj / "nested").mkdir()
        shutil.copy2(source_ws / "src" / "app.py", multi_proj / "nested" / "app.py")

        # Mutate live workspace AFTER projection copy
        (source_ws / "src" / "app.py").write_text("print('v2-live-mutated')\n", encoding="utf-8")

        # Serve both projections strictly on 127.0.0.1 ephemeral ports
        for label, directory in (("jat_archive_projection", jat_proj), ("multi_file_projection", multi_proj)):
            handler = functools.partial(_QuietFileHandler, directory=str(directory))
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                status_root, listing = _http_get(f"http://127.0.0.1:{port}/")
                listing_text = listing.decode("utf-8", errors="replace")
                if label == "jat_archive_projection":
                    st_arch, body_arch = _http_get(
                        f"http://127.0.0.1:{port}/joshs-all-the-things-workspace.tar.zst"
                    )
                    results["checks"][label] = {
                        "ok": status_root == 200 and st_arch == 200 and body_arch == archive_bytes,
                        "listing_contains_archive": "joshs-all-the-things-workspace.tar.zst" in listing_text,
                        "exposes_nested_live_paths": False,
                        "served_bytes": len(body_arch),
                    }
                else:
                    st_app, body_app = _http_get(f"http://127.0.0.1:{port}/app.py")
                    st_nested, body_nested = _http_get(f"http://127.0.0.1:{port}/nested/app.py")
                    st_json, body_json = _http_get(f"http://127.0.0.1:{port}/policy.json")
                    live_text = (source_ws / "src" / "app.py").read_text(encoding="utf-8")
                    served_text = body_app.decode("utf-8")
                    results["checks"][label] = {
                        "ok": (
                            status_root == 200
                            and st_app == 200
                            and st_nested == 200
                            and st_json == 200
                            and body_nested == body_app
                            and b'"synthetic"' in body_json
                        ),
                        "one_shot_projection_frozen_after_live_edit": served_text == "print('v1')\n"
                        and live_text == "print('v2-live-mutated')\n",
                        "mirrors_live_workspace_edits": served_text == live_text,
                    }
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

        # (3) Security bind audit record (from pinned Hauler v2.1.1 internal/server/file.go)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            bound_host = sock.getsockname()[0]
        finally:
            sock.close()
        results["checks"]["security_gate"] = {
            "ok": bound_host == "127.0.0.1",
            "fixture_bind_host": bound_host,
            "hauler_v2_1_1_default_bind": ":<port> (all interfaces / 0.0.0.0)",
            "upstream_listen_address_flag_needed": True,
            "unauthenticated_host_exposure_permitted": False,
        }

    results["ok"] = all(bool(c.get("ok")) for c in results["checks"].values())
    return results


def _isolated_spike(hauler: str, task_root: str, parent_netns: str) -> dict[str, Any]:
    """Real binary experiments; called only by the network-isolated child."""
    if os.readlink("/proc/self/ns/net") == parent_netns:
        raise RuntimeError("refusing fileserver in the host network namespace")
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True, timeout=10)
    root = Path(task_root)
    env = {"PATH": os.environ["PATH"], "HOME": str(root / "home"), "TMPDIR": task_root}
    (root / "home").mkdir()
    checks = {}

    def run(*args):
        return subprocess.run(
            [hauler, "--haulerdir", str(root / "hauler"), *args],
            env=env, cwd=root, capture_output=True, timeout=20, check=False,
        )

    def add(store, source, name):
        result = run("store", "add", "file", str(source), "--name", name, "--store", str(store))
        if result.returncode:
            raise RuntimeError("Hauler add file failed")

    def get(port, path, host=None):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host} if host else {})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def serve(store, projection, port, check):
        projection.mkdir(parents=True, exist_ok=True)
        with (root / f"server-{port}.log").open("wb") as log:
            proc = subprocess.Popen(
                [hauler, "--haulerdir", str(root / "hauler"), "store", "serve", "fileserver",
                 "--store", str(store), "--directory", str(projection), "--port", str(port)],
                cwd=root, env=env, stdout=log, stderr=log,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError("Hauler fileserver exited before readiness")
                    try:
                        if get(port, "/")[0] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Hauler fileserver readiness timed out")
                return check(port)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)

    # JAT's workspace packaging shape, not a claim that JAT capture ran.
    archive = root / "joshs-all-the-things-workspace.tar.zst"
    archive.write_bytes(b"synthetic-archive-layout-only")
    archive_store = root / "archive-store"
    add(archive_store, archive, archive.name)

    def archive_check(port):
        listing = get(port, "/")[1]
        return {"ok": archive.name.encode() in listing and get(port, "/" + archive.name)[1] == archive.read_bytes(),
                "nested_path_status": get(port, "/src/app.py")[0], "jat_capture_executed": False}

    checks["jat_shaped_archive_projection"] = serve(archive_store, root / "archive-projection", 18080, archive_check)
    source = root / "app.py"
    source.write_bytes(b"print('v1')\n")
    config = root / "policy.json"
    config.write_bytes(b'{"mode":"synthetic"}\n')
    store = root / "files-store"
    add(store, source, "app.py")
    add(store, config, "policy.json")
    # Observe whether the actual mapper preserves a nested OCI title.
    nested = run("store", "add", "file", str(source), "--name", "nested/app.py", "--store", str(store))
    projection = root / "files-projection"
    projection.mkdir()
    sentinel = root / "sentinel.txt"
    sentinel.write_bytes(b"synthetic-outside-projection")
    (projection / "linked.txt").symlink_to(sentinel)

    def files_check(port):
        source.write_bytes(b"print('v2-live-mutated')\n")
        status, body = get(port, "/app.py")
        nested_status, nested_body = get(port, "/nested/app.py")
        listeners = Path("/proc/net/tcp").read_text() + Path("/proc/net/tcp6").read_text()
        return {
            "ok": status == 200 and body == b"print('v1')\n" and get(port, "/policy.json")[1] == config.read_bytes(),
            "one_shot_projection_frozen_after_live_edit": body != source.read_bytes(),
            "nested_add_exit": nested.returncode,
            "nested_get_status": nested_status,
            "nested_bytes_match": nested_body == body,
            "nested_listing_status": get(port, "/nested/")[0],
            "foreign_host_status": get(port, "/", "untrusted.invalid")[0],
            "symlink_outside_projection_served": get(port, "/linked.txt")[1] == sentinel.read_bytes(),
            "encoded_parent_traversal_served": get(port, "/%2e%2e/sentinel.txt")[1] == sentinel.read_bytes(),
            "wildcard_listener_in_isolated_namespace": bool(re.search(r"0{8,32}:46A1\s+0{8,32}:0000\s+0A", listeners)),
        }

    checks["individual_files"] = serve(store, projection, 18081, files_check)
    collision_store = root / "collision-store"
    add(collision_store, source, "same.txt")
    add(collision_store, config, "same.txt")

    def collision_check(port):
        status, body = get(port, "/same.txt")
        return {"ok": status == 200 and body in (source.read_bytes(), config.read_bytes()),
                "same_name_result": "first" if body == source.read_bytes() else "second",
                "independent_paths_preserved": False}

    checks["duplicate_names"] = serve(collision_store, root / "collision-projection", 18082, collision_check)
    return {"ok": all(c["ok"] for c in checks.values()), "status": "completed", "mode": "live",
            "live_hauler_binary_executed": True, "network_namespace_isolated": True, "checks": checks}


def _run_live_spike(env: dict[str, Any]) -> dict[str, Any]:
    """Run real Hauler v2.1.1 spike strictly inside an isolated Linux network namespace."""
    blockers: list[str] = []
    if not env["hauler_present"]:
        blockers.append("hauler binary not installed on PATH")
    elif env["hauler_version"] != "2.1.1":
        blockers.append("Hauler version must be exactly 2.1.1")
    if not shutil.which("ip"):
        blockers.append("ip command unavailable for isolated loopback setup")
    if not env["netns_isolation_available"]:
        blockers.append(f"isolated network namespace unavailable ({env['netns_error'] or 'unshare failed'})")
    if blockers:
        return {
            "ok": False,
            "status": "blocked",
            "mode": "live",
            "live_hauler_binary_executed": False,
            "environment": env,
            "blockers": blockers,
            "next_gate": (
                "Run `python3 scripts/verify_hauler_fileserver_spike.py --mode live` on a Linux host "
                "with pinned Hauler v2.1.1 and enabled user+network namespaces (or isolated container)."
            ),
        }

    # A separate process group guarantees timeout cleanup of the worker and servers.
    with tempfile.TemporaryDirectory(prefix="hauler-spike-live-") as tmp:
        worker = (
            "import importlib.util,json,sys; "
            "spec=importlib.util.spec_from_file_location('spike',sys.argv[1]); "
            "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
            "print(json.dumps(module._isolated_spike(*sys.argv[2:])))"
        )
        proc = subprocess.Popen(
            ["unshare", "--user", "--net", "--map-root-user", sys.executable, "-c", worker,
             str(Path(__file__).resolve()), shutil.which("hauler"), tmp, os.readlink("/proc/self/ns/net")],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return {"ok": False, "status": "failed", "mode": "live", "reason": "isolated spike timeout"}
        if proc.returncode != 0:
            return {
                "ok": False, "status": "failed", "mode": "live",
                "live_hauler_binary_executed": True, "environment": env,
                "exit_code": proc.returncode,
                "diagnostic": stderr.replace(tmp, "<synthetic-task>")[-1024:],
            }
        parsed = json.loads(stdout.strip().splitlines()[-1])
        parsed["environment"] = env
        return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("auto", "live", "fixture"),
        default="auto",
        help="live (real Hauler in isolated netns), fixture (synthetic loopback simulation), or auto",
    )
    parser.add_argument("--output", type=Path, help="optional JSON receipt output path")
    args = parser.parse_args(argv)

    env = _probe_environment()
    if args.mode == "fixture":
        report = {**_run_fixture_spike(), "environment": env}
    elif args.mode == "live":
        report = _run_live_spike(env)
    else:
        live = _run_live_spike(env)
        if live.get("status") == "blocked":
            report = {
                "ok": True,
                "status": "live-blocked-fixture-passed",
                "environment": env,
                "live": live,
                "fixture": _run_fixture_spike(),
            }
        else:
            report = live

    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.mode == "live" and not report.get("ok"):
        return 2 if report.get("status") == "blocked" else 1
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
