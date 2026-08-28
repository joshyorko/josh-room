import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from josh_room.cli import build_parser

NODE_PROVIDER_VECTORS = r'''
const [rootPath, templatePath] = process.argv.slice(1);

function vectors(provider) {
  return {
    connection_create: provider.connectionCommand("create", {
      provider: "minio",
      endpoint: "https://minio.example.invalid:9000",
    }),
    connection_list: provider.connectionCommand("list"),
    connection_disconnect: provider.connectionCommand("disconnect", {
      connectionId: "home-minio",
    }),
    connection_reconnect: provider.connectionCommand("reconnect", {
      connectionId: "home-minio",
      endpoint: "https://minio.example.invalid:9000",
    }),
    bucket_list: provider.connectionCommand("list-buckets", {
      connectionId: "home-minio",
    }),
    bucket_create: provider.connectionCommand("create-bucket", {
      connectionId: "home-minio",
      bucket: "room-a",
    }),
    bucket_check: provider.connectionCommand("check-bucket", {
      connectionId: "home-minio",
      bucket: "room-a",
    }),
    r2_bucket_list: provider.bucketCommand("list", {
      provider: "r2",
      dimensionId: "r2",
    }),
    r2_bucket_create: provider.bucketCommand("create", {
      provider: "r2",
      dimensionId: "r2",
      bucket: "josh-room",
    }),
    r2_bucket_check: provider.bucketCommand("check", {
      provider: "r2",
      dimensionId: "r2",
      bucket: "josh-room",
    }),
  };
}

const root = vectors(require(rootPath));
const template = vectors(require(templatePath));
process.stdout.write(JSON.stringify({
  root,
  template,
  root_bytes: Buffer.from(JSON.stringify(root)).toString("base64"),
  template_bytes: Buffer.from(JSON.stringify(template)).toString("base64"),
}));
'''


def _provider_vectors(repo_root):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the extension-provider contract")

    result = subprocess.run(
        [
            node,
            "-e",
            NODE_PROVIDER_VECTORS,
            str(repo_root / "vscode-extension" / "provider.js"),
            str(repo_root / "templates" / "room" / "vscode-extension" / "provider.js"),
        ],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        "Node could not load the provider command surface: "
        f"stdout={result.stdout.decode(errors='replace')!r} "
        f"stderr={result.stderr.decode(errors='replace')!r}"
    )
    return json.loads(result.stdout)


def test_native_provider_dimension_vectors_match_and_parse_in_real_cli():
    repo_root = Path(__file__).parents[1]
    provider_payload = _provider_vectors(repo_root)
    root_vectors = provider_payload["root"]
    template_vectors = provider_payload["template"]

    expected_provider_vectors = {
        "connection_create": [
            "provider", "connection", "create", "--provider", "minio",
            "--endpoint", "https://minio.example.invalid:9000",
        ],
        "connection_list": ["provider", "connection", "list"],
        "connection_disconnect": [
            "provider", "connection", "disconnect", "--connection", "home-minio",
        ],
        "connection_reconnect": [
            "provider", "connection", "reconnect", "--connection", "home-minio",
            "--endpoint", "https://minio.example.invalid:9000",
        ],
        "bucket_list": ["provider", "bucket", "list", "--connection", "home-minio"],
        "bucket_create": [
            "provider", "bucket", "create", "--connection", "home-minio",
            "--bucket", "room-a",
        ],
        "bucket_check": [
        "provider", "bucket", "check", "--connection", "home-minio",
            "--bucket", "room-a",
        ],
        "r2_bucket_list": ["provider", "bucket", "list", "--provider", "r2", "--dimension", "r2"],
        "r2_bucket_create": ["provider", "bucket", "create", "--provider", "r2", "--dimension", "r2", "--bucket", "josh-room"],
        "r2_bucket_check": ["provider", "bucket", "check", "--provider", "r2", "--dimension", "r2", "--bucket", "josh-room"],
    }
    for name, expected in expected_provider_vectors.items():
        assert root_vectors.get(name) == expected, (
            f"root provider command mismatch for {name}: "
            f"expected {expected!r}, got {root_vectors.get(name)!r}"
        )
        assert template_vectors.get(name) == expected, (
            f"template provider command mismatch for {name}: "
            f"expected {expected!r}, got {template_vectors.get(name)!r}"
        )
        if root_vectors.get(name) != template_vectors.get(name):
            raise AssertionError(
                f"root/template provider command mismatch for {name}: "
                f"root={root_vectors.get(name)!r}, template={template_vectors.get(name)!r}"
            )

    assert provider_payload["root_bytes"] == provider_payload["template_bytes"], (
        "root/template provider command vectors are not byte-identical: "
        f"root={base64.b64decode(provider_payload['root_bytes'])!r}, "
        f"template={base64.b64decode(provider_payload['template_bytes'])!r}"
    )

    cli_vectors = {
        **root_vectors,
        "dimension_add": [
            "dimensions", "add", "minio-home-minio-room-a", "--display-name", "room-a",
            "--bucket", "room-a", "--connection", "home-minio",
        ],
        "dimension_select": ["dimensions", "list", "--dimension", "minio-home-minio-room-a"],
        "auth_wait": ["auth", "wait", "session-synthetic", "--dimension", "archive"],
        "auth_cancel": ["auth", "cancel", "session-synthetic", "--json"],
        "complete_hierarchy": [
            "dimensions", "list", "--dimension", "archive", "--with-hierarchy",
        ],
        "hierarchy_projects": ["projects", "list", "--dimension", "archive"],
        "hierarchy_snapshots": [
            "snapshots", "list", "room-a", "--dimension", "archive",
        ],
    }
    parser = build_parser()
    for name, vector in cli_vectors.items():
        try:
            parser.parse_args(vector)
        except SystemExit as error:
            raise AssertionError(
                f"actual josh_room CLI parser rejected {name}: {vector!r} "
                f"(exit {error.code}); root/template/backend command surfaces disagree"
            ) from error
