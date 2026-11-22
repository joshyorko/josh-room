"""Opt-in native turns using the exact files restored by an installed workbench.

The task-private receipt comes from observed registered commands against a real
disposable enrolled provider. It is acceptance evidence, not a product API.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from test_native_claude_continuation import resume_restored_claude
from test_native_codex_continuation import _loopback_namespace, resume_restored_codex

from josh_room.workspace_state import context_status, local_status

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or not os.environ.get("JOSH_ROOM_TEST_INSTALLED_CHAIN_RECEIPT"),
        reason="explicit real installed-reference chain receipt required",
    ),
]


def _private_path(root, value, *, exists=True):
    path = Path(value)
    assert path.is_absolute() and path == path.resolve(strict=exists)
    assert path.is_relative_to(root), "fixture paths must stay in the private task root"
    return path


@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_installed_reference_restore_continues_original_native_id(tmp_path, tool):
    _loopback_namespace()
    receipt_path = Path(os.environ["JOSH_ROOM_TEST_INSTALLED_CHAIN_RECEIPT"])
    assert receipt_path.is_absolute() and receipt_path == receipt_path.resolve()
    assert receipt_path.stat().st_mode & 0o077 == 0
    assert receipt_path.stat().st_size <= 1024 * 1024
    receipt = json.loads(receipt_path.read_text())
    assert receipt["format"] == "josh-room.native-installed-chain.v1"
    assert receipt["runtime"] == "installed-controller-source-override"
    root = Path(receipt["task_root"])
    assert root.is_absolute() and root == root.resolve() and root.stat().st_mode & 0o077 == 0
    assert receipt_path.is_relative_to(root)
    installed = _private_path(root, receipt["installed_extension"])
    archive = _private_path(root, receipt["vsix"])
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == receipt["vsix_sha256"]
    with zipfile.ZipFile(archive) as package:
        for source in (ROOT / "src/josh_room").glob("*.py"):
            relative = "runtime/controller/josh_room/" + source.name
            assert (installed / relative).read_bytes() == source.read_bytes()
            assert package.read("extension/" + relative) == source.read_bytes()
        assert package.read("extension/extension.js") == (ROOT / "vscode-extension/extension.js").read_bytes()
        assert (installed / "extension.js").read_bytes() == (ROOT / "vscode-extension/extension.js").read_bytes()
    case = receipt["cases"][tool]
    assert case["capture_result"] == "captured" and case["restore_result"] == "restored"
    assert case["hydrated"] is True
    workspace = _private_path(root, case["workspace"])
    profile = _private_path(root, case["profile_root"])
    source = _private_path(root, case["source_profile"], exists=False)
    assert not source.exists(), "original source profile must be absent"
    original = _private_path(root, case["original_bytes_path"])
    relative = Path(case["relative"])
    assert not relative.is_absolute() and ".." not in relative.parts
    restored = _private_path(root, str(profile / tool / relative))
    assert restored.read_bytes() == original.read_bytes()
    marker = context_status(workspace)
    assert marker["project_id"] == case["room"] and marker["snapshot_id"] == case["snapshot"]
    assert marker["state"] == "linked" and marker["path_matches"] is True
    assert local_status(workspace)["state"] == "clean"
    if tool == "codex":
        result = resume_restored_codex(tmp_path, workspace=workspace, native_home=profile / tool,
                                       relative=relative, payload=original.read_bytes())
    else:
        result = resume_restored_claude(tmp_path, workspace=workspace, config=profile / tool,
                                        relative=relative, payload=original.read_bytes())
    assert local_status(workspace)["state"] == "clean"
    result.update(installed_registered_restore=True, durable_room_reference=True, cold_room_hydrate=True,
                  artifact_sha256=receipt["vsix_sha256"], runtime_override=receipt["runtime"])
    print(json.dumps(result))
