from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from josh_room.pcc_replay import ReplayCursor, ReplayError, ReplayReader
from josh_room.r2 import evidence_index_key, evidence_object_key
from josh_room.session_evidence import canonical_json

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "session_evidence"


class FakeBackend:
    def __init__(self, entries):
        self.entries = entries
        self.index = {item["index_key"]: item["index_body"] for item in entries}
        self.objects = {item["object_key"]: item["object_body"] for item in entries}

    def discover_evidence_indexes(self, *, max_events, page_size):
        del page_size
        refs = []
        for item in self.entries[:max_events]:
            refs.append(
                {
                    "key": item["index_key"],
                    "ciphertext_sha256": hashlib.sha256(item["index_body"]).hexdigest(),
                    "ciphertext_size": len(item["index_body"]),
                }
            )
        return refs

    def get_evidence_index_bytes(self, key):
        return self.index[key]

    def get_evidence_bytes(self, key, expected_size=None):
        body = self.objects[key]
        if expected_size is not None:
            assert expected_size == len(body)
        return body


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def envelope(document, *, profile="profile-personal", workspace="workspace-synthetic", trusted=True, payload=b""):
    return {
        "manifest": {
            "profile": {"id": profile, "workspace_id": workspace},
            "policy": {"decision": "allow"},
            "producer": {"authenticated": trusted},
        },
        "document": document,
        "payload": payload,
    }


def entry(document, *, index_profile="profile-personal", object_profile="profile-personal", trusted=True, payload=b""):
    object_body = f"object:{document['event_id']}".encode()
    object_digest = hashlib.sha256(object_body).hexdigest()
    index_document = {
        "schema_name": "codex-session-evidence",
        "schema_version": {"major": 1, "minor": 0},
        "kind": "index-event",
        "event_id": f"idx-{document['event_id']}",
        "evidence_kind": document["kind"],
        "evidence_event_id": document["event_id"],
        "content_sha256": document.get("content_sha256", hashlib.sha256(canonical_json(document)).hexdigest()),
        "ciphertext_sha256": object_digest,
        "ciphertext_size": len(object_body),
        "content_type": {
            "session-segment": "application/vnd.josh.codex-session-segment+json",
            "session-final": "application/vnd.josh.codex-session-final+json",
            "session-asset": "application/vnd.josh.codex-session-asset",
        }[document["kind"]],
        "discovery": {"state": "uploaded", "observed_at": "2026-09-22T00:02:00Z"},
    }
    index_body = f"index:{document['event_id']}".encode()
    return {
        "index_key": evidence_index_key(hashlib.sha256(index_body).hexdigest()),
        "index_body": index_body,
        "object_key": evidence_object_key(object_digest),
        "object_body": object_body,
        "index_document": index_document,
        "index_envelope": envelope(index_document, profile=index_profile, trusted=trusted),
        "evidence_envelope": envelope(document, profile=object_profile, trusted=trusted, payload=payload),
    }


def reader(entries, mapping, **kwargs):
    backend = FakeBackend(entries)
    return ReplayReader(
        backend,
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda body, expected_kind=None, **_kwargs: mapping[body],
        **kwargs,
    )


def test_replay_emits_inert_records_and_stable_idempotency():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    index_entry = entry(segment, payload=canonical_json(segment))
    mapping = {
        index_entry["index_body"]: index_entry["index_envelope"],
        index_entry["object_body"]: index_entry["evidence_envelope"],
    }
    first = reader([index_entry], mapping).export(limit=1)
    second = reader([index_entry], mapping).export(limit=1)
    assert first.records[0]["record"]["text"] == "Synthetic public fixture"
    assert first.records[0]["idempotency_key"] == second.records[0]["idempotency_key"]
    assert first.jsonl().__next__().startswith('{"checkpoint"')


def test_cursor_is_consumer_owned_and_replay_from_same_cursor_is_exact():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    mapping = {item["index_body"]: item["index_envelope"], item["object_body"]: item["evidence_envelope"]}
    page = reader([item], mapping).export(limit=1)
    assert page.cursor
    cursor = ReplayCursor.decode(page.cursor, profile_id="profile-personal", destination="private-r2")
    assert cursor.last_index_key == item["index_key"]
    resumed = reader([item], mapping).export(cursor=page.cursor, limit=1)
    assert resumed.records == ()
    assert resumed.complete


def test_cross_profile_and_untrusted_producer_quarantine_without_content():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    wrong = entry(segment, index_profile="profile-work", object_profile="profile-work")
    untrusted = entry(segment, trusted=False)
    mapping = {
        wrong["index_body"]: wrong["index_envelope"],
        wrong["object_body"]: wrong["evidence_envelope"],
        untrusted["index_body"]: untrusted["index_envelope"],
        untrusted["object_body"]: untrusted["evidence_envelope"],
    }
    page = reader([wrong, untrusted], mapping).export(limit=2)
    assert not page.records
    assert {item["reason_code"] for item in page.quarantines} == {"profile-boundary-denied", "untrusted-producer"}
    assert all("Synthetic public fixture" not in json.dumps(item) for item in page.quarantines)


def test_bad_chain_and_missing_asset_are_quarantined():
    first = fixture("golden-session-segment.json")
    first["asset_refs"] = []
    second = fixture("golden-session-segment.json")
    second["event_id"] = "evt-second"
    second["checkpoint"] = {**second["checkpoint"], "start": 1, "end": 2}
    second["previous_segment_sha256"] = "f" * 64
    second["asset_refs"] = [{"asset_id": "missing", "sha256": "a" * 64, "size": 1, "media_category": "other"}]
    second["content_sha256"] = hashlib.sha256(canonical_json(second["records"])).hexdigest()
    second["content_size"] = len(canonical_json(second["records"]))
    first_item = entry(first, payload=canonical_json(first))
    second_item = entry(second, payload=canonical_json(second))
    mapping = {
        first_item["index_body"]: first_item["index_envelope"],
        first_item["object_body"]: first_item["evidence_envelope"],
        second_item["index_body"]: second_item["index_envelope"],
        second_item["object_body"]: second_item["evidence_envelope"],
    }
    page = reader([second_item, first_item], mapping).export(limit=2)
    assert {item["reason_code"] for item in page.quarantines} >= {"broken-chain", "missing-asset"}


def test_unknown_major_and_wrong_ciphertext_digest_quarantine():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    unknown = dict(item)
    unknown_doc = dict(item["index_document"])
    unknown_doc["schema_version"] = {"major": 9, "minor": 0}
    unknown["index_envelope"] = envelope(unknown_doc)
    bad = dict(item)
    bad["index_body"] = b"changed"
    mapping = {
        item["index_body"]: item["index_envelope"],
        item["object_body"]: item["evidence_envelope"],
        unknown["index_body"]: unknown["index_envelope"],
        bad["index_body"]: item["index_envelope"],
        bad["object_body"]: item["evidence_envelope"],
    }
    page = reader([unknown, bad], mapping).export(limit=2)
    assert {item["reason_code"] for item in page.quarantines} >= {"unknown_major", "digest-mismatch"}


def test_identity_required_without_test_decryptor():
    class Backend:
        get_evidence_index_bytes = lambda *_args, **_kwargs: b""
        get_evidence_bytes = lambda *_args, **_kwargs: b""
        discover_evidence_indexes = lambda *_args, **_kwargs: []

    with pytest.raises(ReplayError, match="identity-unavailable"):
        ReplayReader(Backend(), profile_id="profile-personal", destination="private-r2")._decrypt(b"x", expected_kind="index-event")
