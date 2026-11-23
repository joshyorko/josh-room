"""Opt-in installed native history recovery and continuation acceptance.

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
        not sys.platform.startswith("linux"), reason="Linux installed-reference acceptance",
    ),
]


def _private_path(root, value, *, exists=True):
    path = Path(value)
    assert path.is_absolute() and path == path.resolve(strict=exists)
    assert path.is_relative_to(root), "fixture paths must stay in the private task root"
    return path


def _installed_receipt(environment, expected_format):
    receipt_path = Path(os.environ[environment])
    assert receipt_path.is_absolute() and receipt_path == receipt_path.resolve()
    assert receipt_path.stat().st_mode & 0o077 == 0
    assert receipt_path.stat().st_size <= 1024 * 1024
    receipt = json.loads(receipt_path.read_text())
    assert receipt["format"] == expected_format
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
    return receipt, root


@pytest.mark.skipif(not os.environ.get("JOSH_ROOM_TEST_INSTALLED_CHAIN_RECEIPT"),
                    reason="explicit real installed-reference chain receipt required")
@pytest.mark.parametrize("tool", ["codex", "claude"])
def test_installed_reference_restore_continues_original_native_id(tmp_path, tool):
    _loopback_namespace()
    receipt, root = _installed_receipt("JOSH_ROOM_TEST_INSTALLED_CHAIN_RECEIPT", "josh-room.native-installed-chain.v1")
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


@pytest.mark.skipif(not os.environ.get("JOSH_ROOM_TEST_INSTALLED_HISTORY_RECEIPT"),
                    reason="explicit real installed immutable-history receipt required")
@pytest.mark.parametrize("tool", ["codex", "claude"])
@pytest.mark.parametrize("capture", ["a", "b"])
def test_installed_immutable_history_selection_restores_exact_capture(tool, capture):
    receipt, root = _installed_receipt("JOSH_ROOM_TEST_INSTALLED_HISTORY_RECEIPT", "josh-room.native-installed-history.v1")
    case = receipt["cases"][tool]
    first, second = case["capture_receipts"]["a"], case["capture_receipts"]["b"]
    assert first["created"] is True and second["created"] is True
    assert all(isinstance(value, str) and len(value) == 64 and set(value) <= set("0123456789abcdef")
               for value in (first["capture_id"], second["capture_id"]))
    assert first["capture_id"] != second["capture_id"]
    noop = case["noop_receipt"]
    assert noop["noop"] is True and noop["created"] is False
    assert type(noop["data_uploaded"]) is int and noop["data_uploaded"] == 0
    assert noop["capture_id"] == first["capture_id"]
    before = _private_path(root, case["noop_inventory_before"])
    after = _private_path(root, case["noop_inventory_after"])
    assert json.loads(before.read_text()) == json.loads(after.read_text())
    assert case["callbacks"] == {"capture_a": "captured", "capture_noop": "captured",
                                 "capture_b": "captured", "status": "inspected"}
    status = case["status_receipt"]
    assert status["contract_version"] == 2 and status["format"] == "josh-room.agent-references.v2"
    captures = {entry["capture_id"]: entry for entry in status["entries"] if entry["tool"] == tool}
    assert set(captures) == {first["capture_id"], second["capture_id"]}
    assert captures[first["capture_id"]]["content_sha256"] != captures[second["capture_id"]]["content_sha256"]
    assert case["snapshot_ids_before"] == case["snapshot_ids_after"] == [case["snapshot"]]
    workspace = _private_path(root, case["workspace"])
    assert case["hydrated"] is True
    marker = context_status(workspace)
    assert marker["project_id"] == case["room"] and marker["snapshot_id"] == case["snapshot"]
    assert marker["path_matches"] is True and local_status(workspace)["state"] == "clean"
    assert not _private_path(root, case["source_profile"], exists=False).exists()
    restored = case["restores"][capture]
    assert restored["callback"] == "restored"
    selected = case["capture_receipts"][capture]["capture_id"]
    assert restored["verify_receipt"]["capture_id"] == selected
    assert restored["restore_receipt"]["capture_id"] == selected
    assert restored["verify_selection"]["--capture-id"] == selected
    assert restored["restore_selection"]["--capture-id"] == selected
    assert restored["restore_selection"]["--session-id"] == "11111111-1111-4111-8111-111111111111"
    relative = Path(case["relative"])
    assert not relative.is_absolute() and ".." not in relative.parts
    profile = _private_path(root, restored["profile_root"])
    original = _private_path(root, case["originals"][capture])
    target = _private_path(root, str(profile / tool / relative))
    assert target.read_bytes() == original.read_bytes()
    rows = [json.loads(row) for row in target.read_bytes().splitlines()]
    if tool == "codex":
        assert rows[0]["payload"]["id"] == "11111111-1111-4111-8111-111111111111"
        assert rows[0]["payload"]["cwd"] == str(workspace)
    else:
        messages = [row for row in rows if row.get("type") in {"user", "assistant"}]
        assert all(row["sessionId"] == "11111111-1111-4111-8111-111111111111" and row["cwd"] == str(workspace) for row in messages)
    print(json.dumps({"provider": tool, "capture_id": selected, "registered_restore": True,
                      "original_provider_id": "11111111-1111-4111-8111-111111111111", "native_bytes_preserved": True,
                      "history_version": 2, "hosted_acceptance": False, "native_turn_run": False}))
