"""Run small real-engine contracts against a temporary synthetic TLS MinIO.

Supply explicitly verified test binaries. This creates no production buckets,
changes no existing infrastructure and performs no release or CI workflow.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from josh_room.cancellation import terminate_owned_process


def _openssl(*args: str) -> None:
    subprocess.run(
        ["openssl", *args],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )


def run_probe(minio: Path, rustic: Path, restic: Path | None) -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="rustic-minio-contract-") as name:
        fixture = Path(name)
        certs = fixture / "certs"
        certs.mkdir(mode=0o700)
        ca = fixture / "ca.pem"
        ca_key = fixture / "ca.key"
        _openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca),
            "-days",
            "1",
            "-subj",
            "/CN=Synthetic Contract CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        key = certs / "private.key"
        csr = fixture / "server.csr"
        _openssl(
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            "/CN=localhost",
        )
        extensions = fixture / "leaf.ext"
        extensions.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        _openssl(
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(ca),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(certs / "public.crt"),
            "-days",
            "1",
            "-extfile",
            str(extensions),
        )
        key.chmod(0o600)
        ca_key.chmod(0o600)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        endpoint = f"https://localhost:{port}"
        env = {
            "PATH": os.environ["PATH"],
            "MINIO_ROOT_USER": "synthetic-contract-user",
            "MINIO_ROOT_PASSWORD": "synthetic-contract-password",
            "MINIO_BROWSER": "off",
        }
        process = subprocess.Popen(
            [
                str(minio),
                "server",
                str(fixture / "data"),
                "--address",
                f"127.0.0.1:{port}",
                "--certs-dir",
                str(certs),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id="synthetic-contract-user",
                aws_secret_access_key="synthetic-contract-password",
                region_name="us-east-1",
                verify=str(ca),
                config=Config(
                    connect_timeout=1,
                    read_timeout=2,
                    retries={"max_attempts": 0},
                    s3={"addressing_style": "path"},
                ),
            )
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError("temporary MinIO failed to start")
                try:
                    client.create_bucket(Bucket="synthetic-rustic-contract")
                    break
                except (BotoCoreError, ClientError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("temporary MinIO readiness timed out")
            testenv = os.environ.copy()
            testenv.update(
                JOSH_ROOM_RUSTIC_TEST_BINARY=str(rustic),
                JOSH_ROOM_SYNTHETIC_MINIO_ENDPOINT=endpoint,
                JOSH_ROOM_SYNTHETIC_MINIO_CA=str(ca),
            )
            testenv.pop("JOSH_ROOM_RESTIC_TEST_BINARY", None)
            if restic is not None:
                testenv["JOSH_ROOM_RESTIC_TEST_BINARY"] = str(restic)
            return subprocess.run(
                [os.sys.executable, "-m", "pytest", "-q", "tests/test_rustic_live.py"],
                cwd=root,
                env=testenv,
                check=False,
                timeout=180,
            ).returncode
        finally:
            terminate_owned_process(process)
            print("Temporary synthetic MinIO stopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minio", type=Path, required=True)
    parser.add_argument("--rustic", type=Path, required=True)
    parser.add_argument("--restic", type=Path)
    args = parser.parse_args()
    return run_probe(
        args.minio.resolve(strict=True),
        args.rustic.resolve(strict=True),
        args.restic.resolve(strict=True) if args.restic else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
