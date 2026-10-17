"""Actual Linux Rustic/OpenDAL TLS contract, with an owned missing-repo server.

This proves CA scoping and hostname validation only. The HTTP server is a
synthetic protocol fixture, not MinIO, R2 or a live-provider Save/restore test.
"""
import argparse
import hashlib
import http.server
import json
import os
import pathlib
import ssl
import subprocess
import tempfile
import threading

from josh_room.rustic_store import RusticStore
from josh_room.restic_store import ResticStoreError


def run(binary, output):
    result = {"scope": "native Linux TLS and missing-config read only; synthetic HTTP fixture",
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "cases": {}}
    with tempfile.TemporaryDirectory(prefix="rustic-owned-tls-") as name:
        root = pathlib.Path(name)
        def openssl(*args):
            subprocess.run(["openssl", *args], cwd=root, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=15)
        for ca in ("ca", "other"):
            openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "1",
                    "-subj", "/CN=Synthetic " + ca, "-keyout", ca + ".key", "-out", ca + ".pem",
                    "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=localhost",
                "-keyout", "server.key", "-out", "server.csr")
        (root / "leaf.ext").write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost\n")
        openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                "-CAcreateserial", "-sha256", "-days", "1", "-extfile", "leaf.ext", "-out", "server.pem")
        for path in root.glob("*.key"):
            path.chmod(0o600)
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                requests.append(True)
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
            def do_GET(self):
                requests.append(True)
                body = b"<Error><Code>NoSuchKey</Code><Message>synthetic missing config</Message></Error>"
                self.send_response(404)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(root / "server.pem", root / "server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for case, ca, host in (("scoped-ca", "ca", "localhost"),
                                   ("missing-ca", None, "localhost"),
                                   ("wrong-ca", "other", "localhost"),
                                   ("hostname-mismatch", "ca", "127.0.0.1")):
                password = root / (case + ".password")
                password.write_text("synthetic-tls-only-password")
                password.chmod(0o600)
                before = len(requests)
                with RusticStore(executable=str(binary),
                    repository=f"s3:https://{host}:{server.server_port}/synthetic-bucket/repository",
                    password_file=password, cache_dir=root / (case + "-cache"),
                    provider_env={"AWS_ACCESS_KEY_ID": "synthetic-tls-key", "AWS_SECRET_ACCESS_KEY": "synthetic-tls-secret"},
                    ca_bundle=root / (ca + ".pem") if ca else None, command_timeout=5) as store:
                    try:
                        store.open_existing()
                    except ResticStoreError as error:
                        result["cases"][case] = {"code": error.code.value,
                            "http_requests": len(requests) - before,
                            "native_missing_config": bool(store._diagnostics and store._diagnostics.missing)}
                    else:
                        raise AssertionError("admitted missing fixture repository")
                output.write_text(json.dumps(result, indent=2) + "\n")
            assert result["cases"]["scoped-ca"]["native_missing_config"]
            assert result["cases"]["scoped-ca"]["http_requests"] > 0
            for case in ("missing-ca", "wrong-ca", "hostname-mismatch"):
                assert result["cases"][case]["http_requests"] == 0
                assert not result["cases"][case]["native_missing_config"]
            result["result"] = "PASS"
            output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rustic", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    assert os.name == "posix", "Linux-only transport proof"
    run(args.rustic.resolve(strict=True), args.output)
