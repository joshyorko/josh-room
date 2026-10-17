"""Real candidate engine against an owned TLS/S3 protocol fixture.

The fixture does not validate AWS signatures and is not MinIO/R2 acceptance.
It exercises the actual OpenDAL transport, encrypted repository and CA policy.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import shutil
import ssl
import stat
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

from josh_room.private_paths import protect_private_file
from josh_room.restic_store import ResticStoreError
from josh_room.rustic_store import RusticStore


def run(binary: Path, output: Path, candidate_build_root: Path | None = None) -> None:
    result = {"scope": "actual candidate OpenDAL/crypto/metadata over an owned TLS/S3 fixture; signatures not checked",
              "platform": "win32-x64" if os.name == "nt" else "linux-x64",
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "cases": {}}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jr-candidate-tls-") as name:
        root = Path(name)
        openssl = shutil.which("openssl")
        if not openssl and os.name == "nt":
            git = shutil.which("git")
            if git:
                path = Path(git).parent.parent / "usr/bin/openssl.exe"
                if path.is_file(): openssl = str(path)
        if not openssl: raise RuntimeError("native fixture needs OpenSSL for generated certificates")

        def cert(*args):
            subprocess.run([openssl, *args], cwd=root, check=True, timeout=15,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        for ca in ("ca", "other"):
            cert("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "1",
                 "-subj", "/CN=Synthetic " + ca, "-keyout", ca + ".key", "-out", ca + ".pem",
                 "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        cert("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=localhost",
             "-keyout", "server.key", "-out", "server.csr")
        (root / "leaf.ext").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n")
        cert("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
             "-CAcreateserial", "-sha256", "-days", "1", "-extfile", "leaf.ext", "-out", "server.pem")
        for path in root.glob("*.key"): protect_private_file(path)
        objects: dict[str, bytes] = {}
        requests: list[str] = []
        lock = threading.RLock()

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_): pass

            def locate(self):
                parsed = urlsplit(self.path)
                parts = unquote(parsed.path).lstrip("/").split("/", 1)
                if parts[0] != "synthetic-candidate": raise RuntimeError("unexpected fixture bucket")
                with lock: requests.append(self.command)
                return parts[1] if len(parts) == 2 else "", parse_qs(parsed.query, keep_blank_values=True)

            def reply(self, code, body=b"", **headers):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                for key, value in headers.items(): self.send_header(key.replace("_", "-"), str(value))
                self.end_headers()
                if self.command != "HEAD": self.wfile.write(body)

            def do_HEAD(self):
                key, _ = self.locate()
                with lock: value = objects.get(key)
                if not key: self.reply(200)
                elif value is None: self.reply(404)
                else: self.reply(200, value, ETag='"' + hashlib.md5(value, usedforsecurity=False).hexdigest() + '"')

            def do_GET(self):
                key, query = self.locate()
                if "list-type" in query or not key:
                    prefix = query.get("prefix", [""])[0]
                    delimiter = query.get("delimiter", [None])[0]
                    with lock: listing = sorted((k, len(v)) for k, v in objects.items() if k.startswith(prefix))
                    entries, common = [], set()
                    for item, size in listing:
                        suffix = item[len(prefix):]
                        if delimiter and delimiter in suffix:
                            common.add(prefix + suffix.split(delimiter, 1)[0] + delimiter)
                        else:
                            entries.append(f"<Contents><Key>{escape(item)}</Key><LastModified>2026-10-17T00:00:00.000Z</LastModified><Size>{size}</Size><ETag>&quot;synthetic&quot;</ETag><StorageClass>STANDARD</StorageClass></Contents>")
                    body = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>synthetic-candidate</Name><Prefix>'
                            + escape(prefix) + '</Prefix><IsTruncated>false</IsTruncated>' + ''.join(entries)
                            + ''.join('<CommonPrefixes><Prefix>' + escape(p) + '</Prefix></CommonPrefixes>' for p in sorted(common))
                            + '</ListBucketResult>').encode()
                    self.reply(200, body, Content_Type="application/xml")
                    return
                with lock: value = objects.get(key)
                if value is None:
                    self.reply(404, b"<Error><Code>NoSuchKey</Code></Error>", Content_Type="application/xml")
                    return
                bounds = self.headers.get("Range")
                if bounds:
                    first, last = bounds.removeprefix("bytes=").split("-", 1)
                    start = int(first) if first else max(0, len(value) - int(last))
                    end = min(len(value) - 1, int(last)) if first and last else len(value) - 1
                    self.reply(206, value[start:end + 1], Content_Range=f"bytes {start}-{end}/{len(value)}")
                else: self.reply(200, value)

            def do_PUT(self):
                key, _ = self.locate()
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    data = bytearray()
                    while size := int(self.rfile.readline().split(b";", 1)[0], 16):
                        data.extend(self.rfile.read(size)); self.rfile.read(2)
                    while self.rfile.readline().strip(): pass
                    value = bytes(data)
                else: value = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with lock: objects[key] = value
                self.reply(200, ETag='"' + hashlib.md5(value, usedforsecurity=False).hexdigest() + '"')

            def do_DELETE(self):
                key, _ = self.locate()
                with lock: objects.pop(key, None)
                self.reply(204)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(root / "server.pem", root / "server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        password = root / "password"; password.write_text("synthetic-candidate-tls"); protect_private_file(password)

        def store(case, ca, host="localhost"):
            adapter = RusticStore(executable=str(binary), repository=f"s3:https://{host}:{server.server_port}/synthetic-candidate/repository",
                password_file=password, cache_dir=root / (case + "-cache"), ca_bundle=ca, ca_mode="operator",
                metadata_format="nodes-v1", provider_env={"AWS_ACCESS_KEY_ID": "synthetic-key", "AWS_SECRET_ACCESS_KEY": "synthetic-secret"}, command_timeout=20)
            adapter.__enter__()
            # Bound this fixture's negative retries, without altering TLS checks.
            text = adapter._profile.read_text()
            adapter._profile.write_text(text.replace("[repository.options]", '[repository.options]\nretry = "off"'))
            return adapter

        try:
            workspace = root / "workspace"; workspace.mkdir()
            file = workspace / "file"; file.write_bytes(b"original")
            readonly = workspace / "readonly"; readonly.write_bytes(b"readonly"); readonly.chmod(0o444)
            opened = store("positive", root / "ca.pem")
            try:
                opened.initialize(); first = opened.backup(workspace)
                before = file.stat(); file.write_bytes(b"replaced"); os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
                changed = opened.backup(workspace, parent=first.snapshot_id)
                assert changed.snapshot_id and changed.snapshot_id != first.snapshot_id
                assert len(list(opened.entries(changed.snapshot_id))) == 2
                destination = root / "relocated"; opened.restore(changed.snapshot_id, destination)
                assert (destination / "file").read_bytes() == b"replaced"
                assert (destination / "readonly").read_bytes() == b"readonly"
                assert stat.S_IMODE((destination / "readonly").stat().st_mode) == stat.S_IMODE(readonly.stat().st_mode)
                opened.check(read_data=True)
                result["cases"]["scoped-ca-save-restore"] = "PASS"
            finally:
                opened.__exit__(None, None, None)
                readonly.chmod(0o600)
                if (root / "relocated/readonly").exists(): (root / "relocated/readonly").chmod(0o600)
            reopened = store("positive-reopened", root / "ca.pem")
            try:
                reopened.open_existing()
                assert len(list(reopened.entries(changed.snapshot_id))) == 2
                reopened.check(read_data=True)
                result["cases"]["scoped-ca-cold-reopen"] = "PASS"
            finally:
                reopened.__exit__(None, None, None)
            for label, body in (("empty", ""), ("malformed", "not a certificate"),
                                ("invalid-der", "-----BEGIN CERTIFICATE-----\nYWJj\n-----END CERTIFICATE-----"),
                                ("nested-section", "-----BEGIN CERTIFICATE-----\n-----BEGIN PRIVATE KEY-----\nYWJj\n-----END PRIVATE KEY-----\n-----END CERTIFICATE-----"),
                                ("trailing-junk", (root / "ca.pem").read_text() + "junk")):
                (root / (label + ".pem")).write_text(body)
            if candidate_build_root is not None:
                receipt = json.loads((candidate_build_root / "build-receipt.json").read_text())
                probe = candidate_build_root / ("scoped-ca-operator-probe.exe" if os.name == "nt" else "scoped-ca-operator-probe")
                assert hashlib.sha256(binary.read_bytes()).hexdigest() == receipt["binary_sha256"]
                assert hashlib.sha256(probe.read_bytes()).hexdigest() == receipt["operator_probe"]["binary_sha256"]
                env = dict(os.environ, JOSH_ROOM_CANDIDATE_TLS_ENDPOINT=f"https://localhost:{server.server_port}",
                    JOSH_ROOM_CANDIDATE_TLS_FIXTURE=str(root))
                proof = subprocess.run([str(probe)], env=env, capture_output=True, text=True, timeout=60, check=False)
                proof_log = output.with_suffix(".operator-proof.log")
                proof_log.write_text(proof.stdout + proof.stderr)
                assert proof.returncode == 0 and "SCOPED_OPERATOR_ISOLATION_PASS" in proof.stdout
                result["cases"]["same-process-operator-isolation"] = "PASS"
                result["cases"]["strict-invalid-ca-construction"] = "PASS"
            for label, ca, host in (("missing", None, "localhost"), ("wrong", root / "other.pem", "localhost"),
                                   ("hostname", root / "ca.pem", "127.0.0.1"),
                                   *((label, root / (label + ".pem"), "localhost") for label in ("empty", "malformed", "invalid-der", "nested-section", "trailing-junk"))):
                count = len(requests); adapter = None
                try:
                    adapter = store(label, ca, host); adapter.open_existing()
                except ResticStoreError as error:
                    assert len(requests) == count
                    result["cases"][label] = {"result": "PASS rejected", "code": error.code.value, "http_requests": 0}
                else: raise AssertionError("candidate admitted invalid CA/hostname configuration")
                finally:
                    if adapter is not None: adapter.__exit__(None, None, None)
            result["result"] = "PASS"
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustic", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate-build-root", type=Path)
    args = parser.parse_args(); run(args.rustic.resolve(strict=True), args.output, args.candidate_build_root)
