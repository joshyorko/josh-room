"""Legacy archive compatibility and opt-in actual Claude native recapture cycle."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from test_native_claude_acceptance import cold_claude  # noqa: F401
from test_native_claude_continuation import _loopback_namespace, resume_restored_claude

from josh_room import native_agents as agents
from josh_room.crypto import encrypt


def _document(case):
    return json.loads(agents._decrypt_to_memory(case["reference"], case["keys"][:1]))


def test_legacy_claude_v1_archive_keeps_closed_policy_and_cold_restore(request, tmp_path):
    case = request.getfixturevalue("cold_claude")
    document = _document(case)
    assert document["format"] == agents.CLAUDE_FORMAT
    document["format"] = agents.CLAUDE_FORMAT_V1
    legacy = tmp_path / "legacy.age"
    encrypt(json.dumps(document).encode(), case["recipients"], legacy)
    decoded, _ = agents._decode(legacy, case["keys"][:1], document["binding"], "claude")
    assert decoded == {str(case["relative"]): case["payload"]}
    destination = tmp_path / "legacy-cold"
    agents.configure(destination, recipients=case["recipients"], tool="claude", **case["binding"])
    agents.materialize_session(destination, source=legacy, identity_paths=case["keys"][:1],
                               session_id="11111111-1111-4111-8111-111111111111", tool="claude", **case["binding"])
    assert (destination / "claude" / case["relative"]).read_bytes() == case["payload"]


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.environ.get("JOSH_ROOM_TEST_CLAUDE_RECAPTURE") != "1",
    reason="explicit isolated pinned Claude recapture opt-in required",
)
def test_native_turn_recapture_and_second_cold_restore(request, tmp_path):
    _loopback_namespace()
    case = request.getfixturevalue("cold_claude")
    # Reuse the actual restored file; the turn helper does not materialize it.
    shutil.rmtree(case["source"])
    workspace = Path(case["binding"]["workspace"])
    config = case["root"] / "claude"
    result = resume_restored_claude(tmp_path, workspace=workspace, config=config,
                                    relative=case["relative"], payload=case["payload"])
    after = (config / case["relative"]).read_bytes()
    assert after.startswith(case["payload"]) and after != case["payload"]
    reference = tmp_path / "recaptured.age"
    captured = agents.capture(case["root"], output=reference, recipients=case["recipients"],
                               tool="claude", **case["binding"])
    assert captured["ok"] and captured["files"] == 1
    decoded, _ = agents._decode(reference, case["keys"][:1], agents._binding(**case["binding"]), "claude")
    assert decoded == {str(case["relative"]): after}
    # Re-labeling enriched rows as a v1 archive must not broaden legacy policy.
    document = json.loads(agents._decrypt_to_memory(reference, case["keys"][:1]))
    assert document["format"] == agents.CLAUDE_FORMAT
    assert base64.b64decode(document["entries"][0]["data"]) == after
    document["format"] = agents.CLAUDE_FORMAT_V1
    legacy = tmp_path / "invalid-legacy.age"
    encrypt(json.dumps(document).encode(), case["recipients"], legacy)
    with pytest.raises(agents.NativeAgentError):
        agents._decode(legacy, case["keys"][:1], document["binding"], "claude")
    shutil.rmtree(case["root"])
    second = tmp_path / "second-cold"
    agents.configure(second, recipients=case["recipients"], tool="claude", **case["binding"])
    restored = agents.materialize_session(second, source=reference, identity_paths=case["keys"][:1],
        session_id=result["provider_session_id"], tool="claude", **case["binding"])
    assert restored["ok"] and (second / "claude" / case["relative"]).read_bytes() == after
    print(json.dumps({"provider": "claude", "archive_format": agents.CLAUDE_FORMAT,
        "native_turn_observed": True, "recaptured": True, "second_cold_restore": True,
        "rows_preserved": len(after.splitlines()), "sha256": hashlib.sha256(after).hexdigest(),
        "original_provider_id": result["provider_session_id"], "hosted_acceptance": False}))
