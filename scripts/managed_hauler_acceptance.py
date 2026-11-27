"""Replay one exact managed Room/JAT/Hauler OCI and file round trip."""
import contextlib
import gzip
import hashlib
import http.server
import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

OCI = "application/vnd.oci.image"
MEDIA = f"{OCI}.manifest.v1+json"
REPO = "josh-room-synthetic/hauler"
PAYLOAD = b"Josh Room pinned JAT/Hauler acceptance fixture.\n"


def encode(obj):
    return json.dumps(obj, separators=(",", ":")).encode()


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fixture_files():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        member = tarfile.TarInfo("fixture.txt")
        member.size = len(PAYLOAD)
        tar.addfile(member, io.BytesIO(PAYLOAD))
    plain = buffer.getvalue()
    layer = gzip.compress(plain, mtime=0)
    cfg = encode({"architecture": "amd64", "os": "linux",
                  "config": {}, "rootfs": {"type": "layers", "diff_ids": [digest(plain)]}})
    config = {"mediaType": f"{OCI}.config.v1+json", "digest": digest(cfg), "size": len(cfg)}
    layers = [{"mediaType": f"{OCI}.layer.v1.tar+gzip", "digest": digest(layer), "size": len(layer)}]
    manifest = encode({"schemaVersion": 2, "mediaType": MEDIA, "config": config, "layers": layers})
    prefix = f"/v2/{REPO}"
    return digest(manifest), {
        "/v2/": ("application/json", b"{}"),
        f"{prefix}/manifests/check": (MEDIA, manifest),
        f"{prefix}/manifests/{digest(manifest)}": (MEDIA, manifest),
        f"{prefix}/blobs/{digest(cfg)}": (config["mediaType"], cfg),
        f"{prefix}/blobs/{digest(layer)}": (layers[0]["mediaType"], layer),
        "/fixture.txt": ("application/octet-stream", PAYLOAD),
    }


@contextlib.contextmanager
def registry_fixture(rows):
    class Handler(http.server.BaseHTTPRequestHandler):
        def respond(self, include_body):
            match = rows.get(self.path.partition("?")[0])
            if match is None:
                self.send_error(404)
                return
            media, data = match
            self.send_response(200)
            self.send_header("Docker-Distribution-Api-Version", "registry/2.0")
            self.send_header("Docker-Content-Digest", digest(data))
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if include_body:
                self.wfile.write(data)

        def do_GET(self):
            self.respond(True)

        def do_HEAD(self):
            self.respond(False)

        def log_message(self, *_args):
            pass

    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(5)


def diagnostic(path, root):
    # The fixture is synthetic. Retain a short failure tail, excluding local paths.
    data = path.read_bytes()[-8192:].decode("utf-8", errors="replace")
    data = data.replace(str(root), "<fixture>")
    data = re.sub(r"""[A-Za-z]:[\\/][^\r\n"'<>:;,){}\[\]]+""", "<path>", data)
    data = re.sub(r"(?i)(authorization|token|secret|password)\s*[:=]\s*\S+", r"\1=<redacted>", data)
    return data.strip()[-4096:]



# Owner-authorized release limitation, not a successful native OCI pull.
WINDOWS_JAT_ARTIFACT = "sha256:9f09e2e2774d0b91473135af92ad7ec89f1f4244db8be271b0011764622e0bc9"
WINDOWS_HAULER_SHA256 = "c813a4917f20ce96a75ba661ef44144aa917f5582ff23ef72253a65681b583a1"


def known_windows_registry_failure(platform, env, exit_code, result, hauler_sha256):
    details = result.get("diagnostics", "")
    return (
        platform == "nt" and exit_code == 2
        and env.get("JOSH_ROOM_JAT_RELEASE_TAG") == "v0.1.22-jat-runtime"
        and env.get("JOSH_ROOM_JAT_SOURCE_SHA") == "1d757a5ab75e82c4132e6356e382290c31af7ebd"
        and env.get("JOSH_ROOM_JAT_ARTIFACT") == WINDOWS_JAT_ARTIFACT
        and hauler_sha256 == WINDOWS_HAULER_SHA256
        and result.get("operation") == "serve" and result.get("success") is False
        and result.get("exit_status") == 1 and result.get("error") == "jat serve failed with exit 1"
        and isinstance(details, str)
        and re.search(r"Hauler endpoint on port [0-9]+ exited with status 1:", details) is not None
        and "failed to copy config: unexpected status from POST request" in details
        and any(f"/v2/{repo}/blobs/uploads/" in details
                for repo in ("hauler/joshs-all-the-things-workspace.tar.zst", REPO, "hauler/fixture.txt"))
        and "500 Internal Server Error unknown: unknown error" in details
    )


def serve_result(path):
    for line in reversed(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("operation") == "serve":
            return value
    return {}


def verify(root):
    env = dict(os.environ, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    read = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    def command(name, *, jat=False):
        which = "JAT" if jat else "CONTROLLER"
        executable = env["JOSH_ROOM_JAT_RCC_EXE" if jat else "JOSH_ROOM_RCC_EXE"]
        return [executable, "--no-build", "env", "exec", "--artifact", env[f"JOSH_ROOM_{which}_ARTIFACT"],
                "--permissive-local", "--inherit-streams", "--receipt-file", str(root / f"{name}-rcc.json"), "--"]

    def room(name):
        return [*command(name), "python", "-m", "josh_room", "jat"]

    def run(args, name):
        with (root / f"{name}.log").open("wb") as log:
            subprocess.run(args, env=env, cwd=root, stdout=log, stderr=log, check=True, timeout=90)

    expected, rows = fixture_files()
    with registry_fixture(rows) as authority:
        origin = f"{authority}/{REPO}:check"
        source = root / "source"
        source.mkdir()
        (source / "README.md").write_bytes(PAYLOAD)
        images = root / "images.txt"
        images.write_text(origin + "\n", encoding="utf-8")
        manifest = root / "files.yaml"
        manifest.write_text(
            "apiVersion: content.hauler.cattle.io/v1\nkind: Files\nmetadata:\n  name: synthetic\n"
            f"spec:\n  files:\n    - path: http://{authority}/fixture.txt\n      name: fixture.txt\n",
            encoding="utf-8",
        )
        haul = root / "fixture.tar.zst"
        run([*room("capture"), "build", "--source", str(source), "--output", str(haul),
             "--images-file", str(images), "--hauler-manifest", str(manifest), "--exclude-extras", "--json"], "capture")

    startup = root / "startup.json"
    serve_log = root / "serve.log"
    with serve_log.open("wb") as log:
        serve = subprocess.Popen(
            [*room("serve"), "serve", "--haul", str(haul), "--mode", "both", "--startup-file", str(startup), "--json"],
            env=env, cwd=root, stdout=log, stderr=log, start_new_session=os.name != "nt",
        )
        try:
            deadline = time.monotonic() + 80
            while not startup.exists() and serve.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if not startup.is_file():
                failure = serve.poll()
                result = serve_result(serve_log)
                # Verify the actual binary within the already digest-bound JAT artifact.
                hauler_sha256 = ""
                if os.name == "nt" and env.get("JOSH_ROOM_JAT_ARTIFACT") == WINDOWS_JAT_ARTIFACT:
                    identity = subprocess.run(
                        [*command("hauler-identity", jat=True), "python", "-c",
                         ("import hashlib, pathlib, shutil; "
                          "print(hashlib.sha256(pathlib.Path(shutil.which('hauler')).read_bytes()).hexdigest())")],
                        env=env, cwd=root, capture_output=True, text=True, check=True, timeout=30,
                    )
                    hauler_sha256 = identity.stdout.strip()
                if known_windows_registry_failure(os.name, env, failure, result, hauler_sha256):
                    return {"status": "known-limitation", "issue": "hauler-dev/hauler#916",
                            "native_windows_oci_pull": "not-passed", "exit_status": failure,
                            "jat_artifact": WINDOWS_JAT_ARTIFACT, "hauler_sha256": hauler_sha256,
                            "diagnostic": diagnostic(serve_log, root)}
                raise RuntimeError(f"Hauler Serve readiness not recorded; exit={failure}, "
                                   f"diagnostic={diagnostic(serve_log, root) or '(no output)'}")
            ready = json.loads(startup.read_text(encoding="utf-8"))["serve"]
            image, = ready["image_mapping"]["images"]
            pull = image["pull_reference"]
            match = re.fullmatch(rf"(127\.0\.0\.1:[0-9]+)/{REPO}@{expected}", pull)
            if (not match or image["source_reference"] != origin or image["stored_reference"] != origin
                    or image["display_label"] != "hauler:check" or image["digest"] != expected
                    or ready["registry_bind"] != "loopback"):
                raise ValueError("live OCI identity did not match captured origin and digest")
            endpoint = f"http://{match.group(1)}/v2/{REPO}"
            request = urllib.request.Request(f"{endpoint}/manifests/{expected}", headers={"Accept": MEDIA})
            with read(request, timeout=5) as response:
                raw = response.read()
                if response.headers.get("Docker-Content-Digest") != expected or digest(raw) != expected:
                    raise ValueError("registry did not return the exact captured manifest")
            obj = json.loads(raw)
            for blob in [obj["config"], *obj["layers"]]:
                with read(f"{endpoint}/blobs/{blob['digest']}", timeout=5) as response:
                    raw_blob = response.read()
                if len(raw_blob) != blob["size"] or digest(raw_blob) != blob["digest"]:
                    raise ValueError("registry returned an altered OCI blob")
            store = root / "pulled"
            refs = root / "pull.txt"
            refs.write_text(pull + "\n", encoding="utf-8")
            run([*command("pull", jat=True), "hauler", "--store", str(store), "store", "sync",
                 "--image-txt", str(refs), "--exclude-extras"], "pull")
            if (store / "blobs" / "sha256" / expected.split(":")[1]).read_bytes() != raw:
                raise ValueError("real Hauler pull did not store the exact manifest")
            with read(ready["fileserver_url"].rstrip("/") + "/fixture.txt", timeout=5) as response:
                if response.read() != PAYLOAD:
                    raise ValueError("downloaded file bytes differ from capture")
        finally:
            if serve.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(serve.pid), "/T", "/F"],
                                   capture_output=True, check=False, timeout=15)
                else:
                    os.killpg(serve.pid, signal.SIGTERM)
                try:
                    serve.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    if os.name != "nt":
                        os.killpg(serve.pid, signal.SIGKILL)
                    else:
                        serve.kill()
                    serve.wait(timeout=5)
    return {"status": "passed", "image_digest": expected, "file_digest": digest(PAYLOAD),
            "pull_client": "hauler", "registry_bind": "loopback", "fileserver_bind": ready["fileserver_bind"]}


if __name__ == "__main__":
    directory = Path(sys.argv[1]).resolve()
    directory.mkdir(mode=0o700)
    print(json.dumps(verify(directory), sort_keys=True))
