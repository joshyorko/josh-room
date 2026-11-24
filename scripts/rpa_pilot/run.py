"""Own a disposable Linux editor and fresh synthetic fixtures for one UI walk."""
import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from walk import walk

IMAGE = "codercom/code-server@sha256:fc708b18f6c613b5a9d00f1d1801e58376d3df9c5b5de77f5c02d67b711f99c9"


def fixture(root):
    workspace = root / "workspace"
    for name in ("a", "b"):
        child = workspace / "projects" / name
        child.mkdir(parents=True)
        (child / "README.md").write_text(f"Synthetic project {name}\n")
    for i in range(55):
        (workspace / "folders" / f"folder-{i:02}").mkdir(parents=True)
    deep = workspace / "deep"
    for i in range(22):
        deep /= f"level-{i:02}"
    deep.mkdir(parents=True)
    (deep / "README.md").write_text("Synthetic deep project without Git metadata\n")
    (workspace / "nonportable:name").mkdir()
    return workspace


def run(vsix, output, phase, chrome):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix="codex-room-rpa-"))
    name = root.name
    result = {"phase": phase, "image": IMAGE, "cleanup": {}, "verdict": "FAIL"}
    owned = False

    def expired(_signum, _frame):
        raise TimeoutError("Whole pilot deadline exceeded or pilot interrupted")

    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)
    signal.alarm(600)

    def docker(*args):
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, check=True).stdout

    try:
        fixture(root)
        shutil.copyfile(vsix, root / "candidate.vsix")
        result["vsix_sha256"] = hashlib.sha256((root / "candidate.vsix").read_bytes()).hexdigest()
        settings = root / "profile/User/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"security.workspace.trust.enabled": False,
            "workbench.startupEditor": "none", "telemetry.telemetryLevel": "off",
            "update.mode": "none", "extensions.autoUpdate": False}))
        # The extension gets no workstation HOME, credentials, sockets or source mount.
        docker("run", "-d", "--name", name, "--user", f"{os.getuid()}:{os.getgid()}",
               "--memory", "2g", "--cpus", "2", "--pids-limit", "512", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "-p", "127.0.0.1::8080",
               "-v", f"{root}:/pilot:Z", "-e", "HOME=/pilot/home",
               "-e", "XDG_CONFIG_HOME=/pilot/config", "-e", "XDG_CACHE_HOME=/pilot/cache",
               "-e", "XDG_STATE_HOME=/pilot/state", "--entrypoint", "/bin/sh", IMAGE, "-c",
               "mkdir -p /pilot/home; code-server --user-data-dir /pilot/profile --extensions-dir /pilot/extensions "
               "--install-extension /pilot/candidate.vsix && exec code-server --auth none --disable-telemetry "
               "--disable-update-check --bind-addr 0.0.0.0:8080 --user-data-dir /pilot/profile "
               "--extensions-dir /pilot/extensions /pilot/workspace")
        owned = True
        port = int(docker("port", name, "8080/tcp").strip().split(":")[-1])
        deadline = time.monotonic() + 90
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Owned editor startup deadline exceeded")
                time.sleep(0.5)
        result["editor"] = docker("exec", name, "code-server", "--version").strip()
        result["installed"] = docker("exec", name, "code-server", "--extensions-dir", "/pilot/extensions",
                                     "--list-extensions", "--show-versions").strip()
        with zipfile.ZipFile(root / "candidate.vsix") as candidate:
            package = json.loads(candidate.read("extension/package.json"))
            extension = root / "extensions" / f"{package['publisher']}.{package['name']}-{package['version']}"
            checked = 0
            for member in candidate.namelist():
                if not member.startswith("extension/") or member.endswith("/"):
                    continue
                actual = extension / member.removeprefix("extension/")
                expected = candidate.read(member)
                if member == "extension/package.json":
                    installed = json.loads(actual.read_bytes())
                    assert all(installed[k] == v for k, v in package.items())
                    assert set(installed) - set(package) <= {"__metadata"}
                else:
                    assert actual.read_bytes() == expected, f"Installed file differs: {member}"
                checked += 1
            result["installed_files_verified"] = checked
        result["walk"] = walk(f"http://127.0.0.1:{port}/?folder=/pilot/workspace", output / "ui", chrome, phase)
        result["verdict"] = "PASS"
    except Exception as error:
        result["error"] = str(error)
        raise
    finally:
        signal.alarm(0)
        try:
            if owned:
                (output / "editor.log").write_text(docker("logs", name))
                # Only the product channel: no raw environment or generic editor logs.
                for i, log in enumerate(sorted((root / "profile/logs").glob("*/exthost*/joshyorko.josh-room/Josh Room.log"))):
                    shutil.copyfile(log, output / f"controller-{i}.log")
                docker("rm", "-f", name)
                owned = False
                result["cleanup"]["container_removed"] = True
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0, "Editor listener survived cleanup"
                result["cleanup"]["listener_closed"] = True
        except Exception as error:
            result["verdict"] = "FAIL"
            result["cleanup"]["error"] = str(error)
            raise
        finally:
            if owned:
                docker("rm", "-f", name)
            shutil.rmtree(root)
            result["cleanup"]["scratch_removed"] = not root.exists()
            (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vsix", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--phase", choices=["discovery", "catalog"], required=True)
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    args = parser.parse_args()
    run(args.vsix, args.output, args.phase, args.chrome)
