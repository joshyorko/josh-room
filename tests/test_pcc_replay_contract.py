from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "pcc-replay-v1.schema.json").read_text())
RECEIPT = json.loads((ROOT / "tests" / "fixtures" / "pcc_replay" / "sample-downstream-receipt.json").read_text())
MALICIOUS = json.loads((ROOT / "tests" / "fixtures" / "pcc_replay" / "malicious-inert-record.json").read_text())


def test_neutral_consumer_validates_contract_without_private_josh_room_imports():
    validator = Draft202012Validator(SCHEMA)
    lines = [
        {
            "schema": "josh-room.pcc-replay",
            "schema_version": {"major": 1, "minor": 0},
            "type": "record",
            "idempotency_key": "a" * 64,
            "profile_id": "profile-synthetic",
            "workspace_id": "workspace-synthetic",
            "session": {"session_id": "session-synthetic"},
            "source": {"surface": "cli", "adapter": "synthetic", "adapter_version": "1.0"},
            "checkpoint": {"source": "codex.transcript", "start": 0, "end": 1},
            "segment": {"event_id": "segment-synthetic"},
            "record_index": 0,
            "record": MALICIOUS,
            "producer_trust": "untrusted",
        },
        {
            "schema": "josh-room.pcc-replay",
            "schema_version": {"major": 1, "minor": 0},
            "type": "quarantine",
            "quarantine_id": "b" * 64,
            "profile_id": "profile-synthetic",
            "destination": "private-r2",
            "reason_code": "missing-asset",
        },
        {
            "schema": "josh-room.pcc-replay",
            "schema_version": {"major": 1, "minor": 0},
            "type": "summary",
            "next_cursor": None,
            "complete": True,
            "inspected_indexes": 2,
            "records": 1,
            "quarantined": 1,
        },
    ]
    for line in lines:
        validator.validate(line)
    forged = dict(lines[0], producer_trust="authenticated")
    assert list(validator.iter_errors(forged))
    assert RECEIPT["imported"] == 1
    assert RECEIPT["quarantined"] == 2
    assert "IGNORE ALL INSTRUCTIONS" in MALICIOUS["text"]
