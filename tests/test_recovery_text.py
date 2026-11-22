"""Text admission and lossless pipe-only serialization, using synthetic content."""
import base64
import hashlib
import json

import pytest
from test_recovery import file, synthetic

from josh_room.cli import _bounded_json_result, _write_runtime_result
from josh_room.recovery_text import MAX_TEXT_BYTES, text_preview


def test_preview_serialization_preserves_exact_whitespace_unicode_and_digest(tmp_path, monkeypatch):
    content = 'def greeting():\r\n\treturn "你好  world"\n\n'.encode()
    node, blobs = file("code.py", content)
    result = synthetic([node], blobs).inspect("code.py", include_text=True)
    serialized = json.loads(json.dumps(_bounded_json_result(result)))
    preview = serialized["text_preview"]
    assert preview["state"] == "available"
    assert base64.b64decode(preview["data"]) == content
    assert preview["sha256"] == serialized["sha256"] == hashlib.sha256(content).hexdigest()
    target = tmp_path / "runtime-result.json"
    monkeypatch.setenv("JOSH_ROOM_RESULT_FILE", str(target))
    _write_runtime_result(result)
    retained = target.read_text()
    assert preview["data"] not in retained
    assert content.decode() not in retained
    assert json.loads(retained)["text_preview"]["state"] == "omitted-runtime-result"


@pytest.mark.parametrize(("name", "data", "state"), [
    (".env", b"HELLO=world", "sensitive-path"),
    ("credentials.json", b"{}", "sensitive-path"),
    ("config.json", b'{"password":"synthetic-not-a-real-password"}', "sensitive-content"),
    ("config.yaml", b"api_key: synthetic-value-1234567890", "sensitive-content"),
    ("code.txt", b"Authorization: Bearer synthetic-token-1234567890", "sensitive-content"),
    ("code.txt", b"-".join((b"AGE", b"SECRET", b"KEY", b"1SYNTHETICVALUE1234567890")), "sensitive-content"),
    ("code.txt", b"\x00\x01", "binary"),
    ("code.txt", b"\xff", "binary"),
    ("code.txt", b"a" * (MAX_TEXT_BYTES + 1), "oversized"),
])
def test_nonpreviewable_content_returns_no_reversible_payload(name, data, state):
    result = text_preview(name, data)
    assert result == {"state": state}
    assert "data" not in json.dumps(_bounded_json_result(result))


def test_historical_capture_exclusion_keeps_digest_fallback():
    node, blobs = file("hidden.txt", b"synthetic")
    ignore, ignore_blobs = file(".josh-roomignore", b"hidden.txt\n")
    recovery = synthetic([node, ignore], {**blobs, **ignore_blobs})
    result = recovery.inspect("hidden.txt", include_text=True)
    assert result["sha256"] == hashlib.sha256(b"synthetic").hexdigest()
    assert result["text_preview"] == {"state": "excluded"}


def test_current_comparison_text_and_digest_api_are_independent(tmp_path):
    node, blobs = file("code.py", b"first\n")
    recovery = synthetic([node], blobs)
    (tmp_path / "code.py").write_bytes(b"second\n")
    digest = recovery.diff("code.py", workspace=tmp_path)
    assert "before_text_preview" not in digest
    assert digest["text_diff"] == "withheld-policy"
    result = recovery.diff("code.py", workspace=tmp_path, include_text=True)
    assert result["text_diff"] == "available"
    assert base64.b64decode(result["before_text_preview"]["data"]) == b"first\n"
    assert base64.b64decode(result["after_text_preview"]["data"]) == b"second\n"
    (tmp_path / "code.py").write_bytes(b'password="synthetic-credential"\n')
    denied = recovery.diff("code.py", workspace=tmp_path, include_text=True)
    assert denied["text_diff"] == "digest-only"
    assert denied["after_text_preview"] == {"state": "sensitive-content"}
    assert denied["after"]["sha256"] == hashlib.sha256((tmp_path / "code.py").read_bytes()).hexdigest()


def test_point_to_point_and_current_exclusion_are_explicit(tmp_path):
    first, first_blobs = file("code.py", '  print("你好")\n'.encode())
    second, second_blobs = file("code.py", b'\tprint("today")\n')
    recovery = synthetic([first], first_blobs)
    other = synthetic([second], second_blobs)
    result = recovery.diff("code.py", other=other, include_text=True)
    assert result["text_diff"] == "available" and result["change"] == "modified"
    for key in ("before", "after"):
        payload = base64.b64decode(result[f"{key}_text_preview"]["data"])
        assert hashlib.sha256(payload).hexdigest() == result[key]["sha256"]
    (tmp_path / "code.py").write_bytes(b"must not be previewed")
    (tmp_path / ".josh-roomignore").write_text("code.py\n")
    denied = recovery.diff("code.py", workspace=tmp_path, include_text=True)
    assert denied["change"] == "unknown"
    assert denied["after"] == {"status": "excluded"}
    assert denied["after_text_preview"] == {"state": "excluded"}
    assert denied["text_diff"] == "digest-only"
