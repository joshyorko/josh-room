"""One disposable OCI editor target per mission, sharing the pilot fixture contract."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from run import IMAGE, fixture


class Target:
    def __init__(self, vsix, output):
        self.output = output
        self.root = Path(tempfile.mkdtemp(prefix="codex-room-explorer-"))
        self.name = self.root.name
        self.vsix = Path(vsix)
        self.owned = False
        self.port = None
        self.provenance = {"image": IMAGE, "cleanup": {}}
        self.image = os.environ.get("PILOT_PROVIDER_IMAGE", IMAGE)
        if self.image != IMAGE and not self.image.startswith("sha256:"):
            raise ValueError("Provider fixture image must be its exact local content ID")
        self.provider = self.image != IMAGE
        self.provenance["image"] = self.image

    def docker(self, *args):
        process = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
        if process.returncode:
            raise RuntimeError(f"Docker {args[0]} failed: {process.stderr[:500]}")
        return process.stdout

    def __enter__(self):
        try:
            fixture(self.root)
            if self.provider:
                # Save qualification uses a small valid source; discovery keeps its omission fixture.
                workspace = self.root / "workspace"
                for child in ("a", "b"):
                    (workspace / "projects" / child).rename(workspace / child)
                for directory in ("projects", "deep", "folders", "nonportable:name"):
                    shutil.rmtree(workspace / directory)
                binary = Path(os.environ["PILOT_MINIO"])
                assert hashlib.sha256(binary.read_bytes()).hexdigest() == "7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f"
                shutil.copyfile(binary, self.root / "minio")
                (self.root / "minio").chmod(0o700)
                shutil.copyfile(Path(__file__).with_name("provider_fixture.py"), self.root / "provider_fixture.py")
            shutil.copyfile(self.vsix, self.root / "candidate.vsix")
            self.provenance["vsix_sha256"] = hashlib.sha256(self.vsix.read_bytes()).hexdigest()
            with zipfile.ZipFile(self.vsix) as archive:
                self.package = json.loads(archive.read("extension/package.json"))
            settings = self.root / "profile/User/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"security.workspace.trust.enabled": False,
                "workbench.startupEditor": "none", "telemetry.telemetryLevel": "off", "update.mode": "none",
                "extensions.autoUpdate": False, "extensions.autoCheckUpdates": False}))
            self.docker("run", "-d", "--name", self.name, "--user", f"{os.getuid()}:{os.getgid()}",
                "--memory", "2g", "--cpus", "2", "--pids-limit", "512", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "-p", "127.0.0.1::8080", "-v", f"{self.root}:/pilot:Z",
                "-e", "HOME=/pilot/home", "-e", "XDG_CONFIG_HOME=/pilot/config", "-e", "XDG_CACHE_HOME=/pilot/cache",
                "-e", "XDG_STATE_HOME=/pilot/state", "-e", "DBUS_SESSION_BUS_ADDRESS=unix:path=/pilot/keyring-runtime/bus",
                "--entrypoint", "/bin/sh", self.image, "-c",
                "mkdir -p /pilot/home; if [ -x /usr/bin/gnome-keyring-daemon ]; then "
                "mkdir -m 700 /pilot/keyring-runtime; dbus-daemon --session --address=unix:path=/pilot/keyring-runtime/bus --fork; "
                "printf 'synthetic-disposable-unlock\\n' | gnome-keyring-daemon --unlock --components=secrets "
                "--control-directory=/pilot/keyring-runtime/control >/pilot/keyring-service.log 2>&1; fi; "
                "code-server --user-data-dir /pilot/profile --extensions-dir /pilot/extensions "
                "--install-extension /pilot/candidate.vsix && exec code-server --auth none --disable-telemetry "
                "--disable-update-check --bind-addr 0.0.0.0:8080 --user-data-dir /pilot/profile "
                "--extensions-dir /pilot/extensions /pilot/workspace")
            self.owned = True
            self.port = int(self.docker("port", self.name, "8080/tcp").strip().split(":")[-1])
            self.provenance.update(loopback_port=self.port, session_id=self.name)
            deadline = time.monotonic() + 90
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Editor startup")
                    time.sleep(0.5)
            self.url = f"http://127.0.0.1:{self.port}/?folder=/pilot/workspace"
            installed = self.root / "extensions" / f"{self.package['publisher']}.{self.package['name']}-{self.package['version']}"
            count = 0
            with zipfile.ZipFile(self.vsix) as archive:
                for member in archive.namelist():
                    if not member.startswith("extension/") or member.endswith("/"):
                        continue
                    raw = archive.read(member)
                    actual = (installed / member.removeprefix("extension/")).read_bytes()
                    if member == "extension/package.json":
                        expected, observed = json.loads(raw), json.loads(actual)
                        assert all(observed[k] == v for k, v in expected.items())
                        assert set(observed) - set(expected) <= {"__metadata"}
                    else:
                        assert raw == actual, member
                    count += 1
            self.provenance.update(installed_files_verified=count,
                extension_version=self.package["version"], editor=self.docker("exec", self.name, "code-server", "--version").strip(),
                node=self.docker("exec", self.name, "/usr/lib/code-server/lib/node", "--version").strip())
            if self.provider:
                for args in [("init", "-b", "main"), ("add", "README.md"),
                             ("-c", "user.name=Synthetic Pilot", "-c", "user.email=synthetic@example.invalid", "commit", "-m", "Synthetic fixture")]:
                    self.docker("exec", self.name, "git", "-C", "/pilot/workspace/a", *args)
                # Reuse catalog_repository's existing synthetic metadata exclusions.
                with (self.root / "workspace/a/.git/info/exclude").open("a") as stream:
                    stream.write("\n/.josh-room.json\n/.josh-room-split.json\n/.josh-roomignore\n")
            (self.output / "surface.json").write_text(json.dumps({"schema_version": 1,
                "contributes": self.package["contributes"], "activationEvents": self.package["activationEvents"],
                "coverage": {row["command"]: "NOT RUN" for row in self.package["contributes"]["commands"]}}, indent=2))
            self.save()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def save(self):
        (self.output / "target.json").write_text(json.dumps(self.provenance, indent=2) + "\n")

    def fixture_operation(self, mode):
        if not self.provider or mode not in {"enroll", "cold_restore", "verify_child"}:
            raise ValueError("Synthetic provider operation unavailable")
        base = "/pilot/profile/User/globalStorage/joshyorko.josh-room"
        manifest = json.loads((self.root / f"extensions/joshyorko.josh-room-{self.package['version']}/runtime/manifest.json").read_text())
        rcc = f"{base}/runtime/rcc/{manifest['rcc']['version']}/linux-x64/rcc"
        controller = f"/pilot/extensions/joshyorko.josh-room-{self.package['version']}/runtime/controller"
        environment = {"ROBOCORP_HOME": base + "/robocorp", "RCC_HOLOTREE_MODE": "private",
            "PYTHONPATH": controller, "JOSH_ROOM_CONFIG_DIR": base + "/config", "JOSH_ROOM_INSTANCE": base + "/state/josh-room",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/pilot/keyring-runtime/bus", "JOSH_ROOM_EXTENSION_MODE": "0"}
        flags = [part for key, value in environment.items() for part in ("-e", key + "=" + value)]
        command = [rcc, "--no-build", "env", "exec", "--artifact", manifest["controller"]["environment_artifacts"]["linux-x64"]["digest"],
                   "--permissive-local", "--receipt-file", f"/pilot/fixture-{mode}-rcc.json", "--inherit-streams", "--json",
                   "--", "python", "/pilot/provider_fixture.py", mode]
        if mode == "enroll":
            self.docker("exec", "-d", *flags, self.name, "/bin/sh", "-c", 'exec "$@" >/pilot/fixture-enroll.log 2>&1', "sh", *command)
            deadline = time.monotonic() + 150
            while not (self.root / "provider-ready.json").exists():
                if (self.root / "fixture-failure.json").exists():
                    raise RuntimeError((self.root / "fixture-failure.json").read_text())
                if time.monotonic() >= deadline:
                    raise TimeoutError("Synthetic managed enrollment readiness")
                time.sleep(0.5)
            filename = "provider-ready.json"
        else:
            self.docker("exec", *flags, self.name, *command)
            filename = "cold-receipt.json" if mode == "cold_restore" else "hydration-receipt.json"
        receipt = json.loads((self.root / filename).read_text())
        (self.output / filename).write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt

    def __exit__(self, *_):
        try:
            if self.owned:
                try:
                    for name in ["provider-ready.json", "fixture-failure.json", "cold-receipt.json", "hydration-receipt.json", "fixture-enroll-rcc.json", "fixture-cold_restore-rcc.json", "fixture-verify_child-rcc.json"]:
                        if (self.root / name).exists():
                            shutil.copyfile(self.root / name, self.output / name)
                    for number, log in enumerate(sorted((self.root / "profile/logs").glob("*/exthost*/joshyorko.josh-room/Josh Room.log"))):
                        shutil.copyfile(log, self.output / f"product-{number}.log")
                finally:
                    self.docker("rm", "-f", self.name)
                    self.owned = False
                    self.provenance["cleanup"]["container_removed"] = True
            if self.port:
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", self.port)) != 0
                self.provenance["cleanup"]["listener_closed"] = True
        finally:
            shutil.rmtree(self.root)
            self.provenance["cleanup"]["scratch_removed"] = not self.root.exists()
            self.save()
