"""Small, opt-in historical text envelopes using existing safety rules."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from .material_security import _CONTENT_PATTERNS, _path_class

MAX_TEXT_BYTES = 2048  # Base64 fits the existing 4096-character JSON value bound.
_ASSIGNMENT = re.compile(rb"([A-Za-z][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*[^\s,;}]+")


def text_preview(path, data):
    """Never return matched sensitive values, partial text or undecodable bytes."""
    from .cli import (
        _JSON_SENSITIVE_KEYS,
        _sanitize_human_diagnostic,
        _sanitize_json_text,
    )

    if _path_class(Path(path)) is not None:
        return {"state": "sensitive-path"}
    if len(data) > MAX_TEXT_BYTES:
        return {"state": "oversized"}
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return {"state": "binary"}
    if any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in text):
        return {"state": "binary"}
    if any(pattern.search(data) for pattern in _CONTENT_PATTERNS):
        return {"state": "sensitive-content"}
    for match in _ASSIGNMENT.finditer(data):
        key = match[1].decode("ascii").lower()
        if (key in _JSON_SENSITIVE_KEYS or key in {"api_key", "api-key", "apikey", "authorization"}
                or any(part in key for part in ("secret", "password", "token"))):
            return {"state": "sensitive-content"}
    sanitized = _sanitize_human_diagnostic(text) + _sanitize_json_text(text)
    if any(marker in sanitized for marker in ("[REDACTED]", "[REDACTED AGE IDENTITY]", "[REDACTED AGE RECIPIENT]")):
        return {"state": "sensitive-content"}
    return {"state": "available", "encoding": "base64-utf8", "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "data": base64.b64encode(data).decode("ascii")}


def without_text_envelopes(value):
    """Runtime receipts retain metadata only, even for admitted nonsensitive text."""
    if isinstance(value, dict):
        if value.get("encoding") == "base64-utf8":
            return {**{key: entry for key, entry in value.items() if key != "data"},
                    "state": "omitted-runtime-result"}
        return {key: without_text_envelopes(entry) for key, entry in value.items()}
    if isinstance(value, (list, tuple)):
        return [without_text_envelopes(entry) for entry in value]
    return value
