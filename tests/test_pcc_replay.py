from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from josh_room.pcc_crypto import CryptoError, CryptoErrorCode
from josh_room.pcc_replay import ReplayCursor, ReplayError, ReplayLimits, ReplayReader
from josh_room.r2 import R2EvidenceError, evidence_index_key, evidence_object_key
from josh_room.session_evidence import canonical_digest, canonical_json

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "session_evidence"


class FakeBackend:
    def __init__(self, entries):
        self.entries = entries
        self.index = {item["index_key"]: item["index_body"] for item in entries}
        self.objects = {item["object_key"]: item["object_body"] for item in entries}

    def discover_evidence_indexes(self, *, max_events, page_size, max_pages):
        del page_size, max_pages
        refs = []
        for item in self.entries[:max_events]:
            digest = item["index_key"].rsplit("/", 1)[-1].removesuffix(".age")
            refs.append(
                {
                    "key": item["index_key"],
                    "ciphertext_sha256": digest,
                    "ciphertext_size": len(item["index_body"]),
                }
            )
        return refs

    def get_evidence_index_bytes(self, key, expected_size=None):
        body = self.index[key]
        if expected_size is not None:
            assert expected_size == len(body)
        return body

    def get_evidence_bytes(self, key, expected_size=None):
        body = self.objects[key]
        if expected_size is not None:
            assert expected_size == len(body)
        return body


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def envelope(
    document,
    *,
    profile="profile-personal",
    workspace="workspace-synthetic",
    producer_claim=True,
    payload=b"",
):
    capture = document.get("capture")
    policy = {
        "decision": capture.get("policy_decision", "allow") if isinstance(capture, dict) else "allow",
        "status": capture.get("status", "complete") if isinstance(capture, dict) else "complete",
        "sensitivity": capture.get("sensitivity", "unknown") if isinstance(capture, dict) else "unknown",
    }
    manifest = {"profile": {"id": profile, "workspace_id": workspace}, "policy": policy}
    if producer_claim is not None:
        manifest["producer"] = {"authenticated": producer_claim}
    return {"manifest": manifest, "document": document, "payload": payload}


def entry(document, *, index_profile="profile-personal", object_profile="profile-personal", producer_claim=True, payload=b""):
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
        "index_envelope": envelope(index_document, profile=index_profile, producer_claim=producer_claim),
        "evidence_envelope": envelope(document, profile=object_profile, producer_claim=producer_claim, payload=payload),
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
    malicious = json.loads((ROOT / "tests" / "fixtures" / "pcc_replay" / "malicious-inert-record.json").read_text())
    segment["records"] = [malicious]
    records_payload = canonical_json(segment["records"])
    segment["content_sha256"] = hashlib.sha256(records_payload).hexdigest()
    segment["content_size"] = len(records_payload)
    index_entry = entry(segment, payload=canonical_json(segment))
    mapping = {
        index_entry["index_body"]: index_entry["index_envelope"],
        index_entry["object_body"]: index_entry["evidence_envelope"],
    }

    first = reader([index_entry], mapping).export(limit=1)
    second = reader([index_entry], mapping).export(limit=1)
    first_jsonl = tuple(first.jsonl())

    assert first.records[0]["record"] == malicious
    assert first.records[0]["idempotency_key"] == second.records[0]["idempotency_key"]
    assert first_jsonl == tuple(second.jsonl())
    assert json.loads(first_jsonl[0])["record"]["tool_output"] == malicious["tool_output"]


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

@pytest.mark.parametrize("final_matches", [True, False])
def test_partial_cursor_resume_validates_full_segment_asset_final_chain(final_matches):
    segment = fixture("golden-session-segment.json")
    asset = fixture("golden-session-asset.json")
    segment["session_id"] = asset["session_id"] = "session-resume"
    asset_payload = b"synthetic asset payload"
    asset["sha256"] = hashlib.sha256(asset_payload).hexdigest()
    asset["size"] = len(asset_payload)
    segment["asset_refs"] = [{
        "asset_id": asset["asset_id"],
        "sha256": asset["sha256"],
        "size": asset["size"],
        "media_category": asset["media_category"],
    }]
    final = fixture("golden-session-final.json")
    final["session_id"] = "session-resume"
    final["last_segment_sha256"] = canonical_digest(segment) if final_matches else "f" * 64
    segment_entry = entry(segment, payload=canonical_json(segment))
    asset_entry = entry(asset, payload=asset_payload)
    final_entry = entry(final, payload=canonical_json(final))
    entries = [final_entry, asset_entry, segment_entry]
    mapping = {
        item["index_body"]: item["index_envelope"]
        for item in entries
    } | {
        item["object_body"]: item["evidence_envelope"]
        for item in entries
    }

    cursor = None
    pages = []
    for _ in entries:
        page = reader(entries, mapping).export(cursor=cursor, limit=1)
        pages.append(page)
        cursor = page.cursor
        if page.complete:
            break
    records = [record for page in pages for record in page.records]
    quarantines = [item for page in pages for item in page.quarantines]

    if final_matches:
        assert len(records) == 1
        assert not quarantines
    else:
        assert not records
        assert any(item["reason_code"] == "broken-chain" for item in quarantines)


def test_segment_predecessor_chain_survives_partial_cursor_resume():
    first = fixture("golden-session-segment.json")
    first["session_id"] = "session-linked"
    first["asset_refs"] = []
    second = fixture("golden-session-segment.json")
    second["event_id"] = "evt-second"
    second["session_id"] = "session-linked"
    second["checkpoint"] = {**second["checkpoint"], "start": 1, "end": 2}
    second["previous_segment_sha256"] = canonical_digest(first)
    second["asset_refs"] = []
    first_entry = entry(first, payload=canonical_json(first))
    second_entry = entry(second, payload=canonical_json(second))
    entries = [second_entry, first_entry]
    mapping = {
        item["index_body"]: item["index_envelope"]
        for item in entries
    } | {
        item["object_body"]: item["evidence_envelope"]
        for item in entries
    }

    cursor = None
    pages = []
    for _ in entries:
        page = reader(entries, mapping).export(cursor=cursor, limit=1)
        pages.append(page)
        cursor = page.cursor
        if page.complete:
            break

    assert {record["segment"]["event_id"] for page in pages for record in page.records} == {
        "evt-cli-001",
        "evt-second",
    }
    assert all(not page.quarantines for page in pages)
def test_duplicate_and_out_of_order_discovery_exports_each_event_once():
    items = []
    mapping = {}
    for event_id in ("evt-one", "evt-two"):
        segment = fixture("golden-session-segment.json")
        segment["event_id"] = event_id
        segment["session_id"] = f"session-{event_id}"
        segment["asset_refs"] = []
        item = entry(segment, payload=canonical_json(segment))
        items.append(item)
        mapping[item["index_body"]] = item["index_envelope"]
        mapping[item["object_body"]] = item["evidence_envelope"]

    page = reader([items[1], items[0], items[1]], mapping).export(limit=8)

    assert {item["segment"]["event_id"] for item in page.records} == {"evt-one", "evt-two"}
    assert len(page.records) == 2
    assert not page.quarantines
def test_multi_session_quarantine_jsonl_is_stably_sorted():
    for attempt in range(100):
        session_ids = [f"session-order-{attempt}-{suffix}" for suffix in ("a", "b")]
        items = []
        mapping = {}
        event_sessions = {}
        for index, session_id in enumerate(session_ids):
            segment = fixture("golden-session-segment.json")
            event_id = f"event-order-{attempt}-{index}"
            segment["event_id"] = event_id
            segment["session_id"] = session_id
            segment["previous_segment_sha256"] = "f" * 64
            segment["asset_refs"] = []
            item = entry(segment, payload=canonical_json(segment))
            items.append(item)
            mapping[item["index_body"]] = item["index_envelope"]
            mapping[item["object_body"]] = item["evidence_envelope"]
            event_sessions[event_id] = session_id
        ordered = sorted(items, key=lambda item: item["index_key"])
        session_order = list({
            item["evidence_envelope"]["document"]["session_id"]: []
            for item in ordered
        }.keys() | {}.keys())
        if session_order != sorted(session_ids):
            break
    else:
        pytest.fail("could not generate a nondeterministic-order witness")

    page = reader(items, mapping).export(limit=8)
    quarantine_sessions = [
        event_sessions[item["event_id"]]
        for line in page.jsonl()
        if (item := json.loads(line)).get("type") == "quarantine"
    ]

    assert quarantine_sessions == sorted(session_ids)

def test_cross_profile_quarantine_without_content():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    wrong = entry(segment, index_profile="profile-work", object_profile="profile-work")
    mapping = {
        wrong["index_body"]: wrong["index_envelope"],
        wrong["object_body"]: wrong["evidence_envelope"],
    }
    page = reader([wrong], mapping).export(limit=1)
    assert not page.records
    assert {item["reason_code"] for item in page.quarantines} == {"profile-boundary-denied"}
    assert all("Synthetic public fixture" not in json.dumps(item) for item in page.quarantines)


@pytest.mark.parametrize("producer_claim", [None, False, True])
def test_deferred_signatures_preserve_evidence_as_untrusted(producer_claim):
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, producer_claim=producer_claim, payload=canonical_json(segment))
    mapping = {
        item["index_body"]: item["index_envelope"],
        item["object_body"]: item["evidence_envelope"],
    }

    page = reader([item], mapping).export(limit=1)

    assert len(page.records) == 1
    assert page.records[0]["producer_trust"] == "untrusted"
    assert not page.quarantines
    assert all("signature" not in value["manifest"] for value in (
        item["index_envelope"],
        item["evidence_envelope"],
    ))

@pytest.mark.parametrize("scope_location", ["index", "evidence"])
def test_cross_workspace_quarantine_without_content(scope_location):
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment)
    item[f"{scope_location}_envelope"]["manifest"]["profile"]["workspace_id"] = "workspace-work"
    mapping = {
        item["index_body"]: item["index_envelope"],
        item["object_body"]: item["evidence_envelope"],
    }

    page = reader([item], mapping).export(limit=1)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} == {"profile-boundary-denied"}
@pytest.mark.parametrize(
    ("manifest_decision", "capture_decision"),
    (("deny", "deny"), ("quarantine", "quarantine"), ("allow", "deny"), ("allow", "quarantine")),
)
def test_evidence_policy_must_be_allowed_and_match_capture(manifest_decision, capture_decision):
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    segment["capture"]["policy_decision"] = capture_decision
    item = entry(segment, payload=canonical_json(segment))
    item["evidence_envelope"]["manifest"]["policy"]["decision"] = manifest_decision
    mapping = {
        item["index_body"]: item["index_envelope"],
        item["object_body"]: item["evidence_envelope"],
    }

    page = reader([item], mapping).export(limit=1)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} == {"policy-mismatch"}


def test_denied_asset_cannot_satisfy_segment_reference():
    segment = fixture("golden-session-segment.json")
    asset = fixture("golden-session-asset.json")
    segment["session_id"] = asset["session_id"] = "session-assets"
    asset_payload = b"synthetic asset payload"
    asset["sha256"] = hashlib.sha256(asset_payload).hexdigest()
    asset["size"] = len(asset_payload)
    asset["capture"]["policy_decision"] = "deny"
    segment["asset_refs"] = [{
        "asset_id": asset["asset_id"],
        "sha256": asset["sha256"],
        "size": asset["size"],
        "media_category": asset["media_category"],
    }]
    segment_entry = entry(segment, payload=canonical_json(segment))
    asset_entry = entry(asset, payload=asset_payload)
    mapping = {
        segment_entry["index_body"]: segment_entry["index_envelope"],
        segment_entry["object_body"]: segment_entry["evidence_envelope"],
        asset_entry["index_body"]: asset_entry["index_envelope"],
        asset_entry["object_body"]: asset_entry["evidence_envelope"],
    }

    page = reader([segment_entry, asset_entry], mapping).export(limit=2)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} >= {"policy-mismatch", "missing-asset"}
def test_asset_reference_category_must_match_asset_event():
    segment = fixture("golden-session-segment.json")
    asset = fixture("golden-session-asset.json")
    segment["session_id"] = asset["session_id"] = "session-assets"
    asset_payload = b"synthetic asset payload"
    asset["sha256"] = hashlib.sha256(asset_payload).hexdigest()
    asset["size"] = len(asset_payload)
    segment["asset_refs"] = [{
        "asset_id": asset["asset_id"],
        "sha256": asset["sha256"],
        "size": asset["size"],
        "media_category": asset["media_category"],
    }]
    asset["media_category"] = "other"
    segment_entry = entry(segment, payload=canonical_json(segment))
    asset_entry = entry(asset, payload=asset_payload)
    mapping = {
        segment_entry["index_body"]: segment_entry["index_envelope"],
        segment_entry["object_body"]: segment_entry["evidence_envelope"],
        asset_entry["index_body"]: asset_entry["index_envelope"],
        asset_entry["object_body"]: asset_entry["evidence_envelope"],
    }

    page = reader([segment_entry, asset_entry], mapping).export(limit=2)

    assert not page.records
    assert "missing-asset" in {receipt["reason_code"] for receipt in page.quarantines}



@pytest.mark.parametrize(
    ("page_size", "max_indexes", "max_ciphertext_bytes", "max_scan_bytes"),
    [
        (9, 1000, ReplayLimits().max_ciphertext_bytes, ReplayLimits().max_scan_bytes),
        (8, 100_001, ReplayLimits().max_ciphertext_bytes, ReplayLimits().max_scan_bytes),
        (8, 1000, ReplayLimits().max_ciphertext_bytes + 1, ReplayLimits().max_scan_bytes),
        (8, 1000, ReplayLimits().max_ciphertext_bytes, 8 * 1024 * 1024 * 1024 + 1),
    ],
)
def test_replay_limits_reject_unbounded_settings(page_size, max_indexes, max_ciphertext_bytes, max_scan_bytes):
    with pytest.raises(ValueError):
        ReplayLimits(
            page_size=page_size,
            max_indexes=max_indexes,
            max_ciphertext_bytes=max_ciphertext_bytes,
            max_scan_bytes=max_scan_bytes,
        )


def test_replay_limits_preserve_original_discovery_capacity():
    limits = ReplayLimits(max_indexes=100_000, max_scan_bytes=8 * 1024 * 1024 * 1024)

    assert limits.max_indexes == 100_000
    assert limits.max_scan_bytes == 8 * 1024 * 1024 * 1024

def test_oversized_index_is_rejected_before_ciphertext_fetch():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    digest = item["index_key"].rsplit("/", 1)[-1].removesuffix(".age")

    class Backend:
        discover_evidence_indexes = lambda *_args, **_kwargs: [{
            "key": item["index_key"],
            "ciphertext_sha256": digest,
            "ciphertext_size": ReplayLimits().max_ciphertext_bytes + 1,
        }]
        get_evidence_index_bytes = lambda *_args, **_kwargs: pytest.fail("oversized index fetched")
        get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("evidence fetched without index")

    page = ReplayReader(
        Backend(),
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda *_args, **_kwargs: pytest.fail("oversized index decrypted"),
    ).export(limit=1)

    assert {item["reason_code"] for item in page.quarantines} == {"ciphertext-too-large"}


def test_oversized_advertised_evidence_size_is_quarantined_before_scan_budget():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    item["index_envelope"]["document"]["ciphertext_size"] = ReplayLimits().max_scan_bytes + 1
    backend = FakeBackend([item])
    backend.get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("oversized evidence fetched")
    mapping = {item["index_body"]: item["index_envelope"]}

    page = ReplayReader(
        backend,
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda body, **_kwargs: mapping[body],
    ).export(limit=1)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} == {"ciphertext-size-invalid"}
    assert page.complete is False
    assert page.cursor is None


def test_metadata_only_inspection_never_fetches_or_decrypts():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    backend = FakeBackend([item])
    backend.get_evidence_index_bytes = lambda *_args, **_kwargs: pytest.fail("inspect fetched ciphertext")
    backend.get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("inspect fetched evidence")

    result = ReplayReader(
        backend,
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda *_args, **_kwargs: pytest.fail("inspect decrypted ciphertext"),
    ).inspect(limit=1)

    assert result["metadata_only"] is True
    assert result["indexes"] == [{
        "key": item["index_key"],
        "ciphertext_sha256": item["index_key"].rsplit("/", 1)[-1].removesuffix(".age"),
        "ciphertext_size": len(item["index_body"]),
    }]

def test_corrupt_ciphertext_is_quarantined_without_content():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    backend = FakeBackend([item])

    def fail_decrypt(*_args, **_kwargs):
        raise ValueError("bad age data")

    page = ReplayReader(
        backend,
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=fail_decrypt,
    ).export(limit=1)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} == {"corrupt-ciphertext"}


@pytest.mark.parametrize("replacement", [b"x", b"x" * len(b"object:evt-cli-001")])
def test_evidence_size_and_digest_mismatches_are_quarantined(replacement):
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    backend = FakeBackend([item])
    backend.get_evidence_bytes = lambda *_args, **_kwargs: replacement
    mapping = {item["index_body"]: item["index_envelope"]}
    replay_reader = ReplayReader(
        backend,
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda body, **_kwargs: mapping[body],
    )

    page = replay_reader.export(limit=1)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} == {"digest-mismatch"}
def test_index_cap_does_not_export_an_unverified_partial_set():
    items = []
    mapping = {}
    for event_id in ("evt-one", "evt-two", "evt-three"):
        segment = fixture("golden-session-segment.json")
        segment["event_id"] = event_id
        segment["session_id"] = f"session-{event_id}"
        segment["asset_refs"] = []
        item = entry(segment, payload=canonical_json(segment))
        items.append(item)
        mapping[item["index_body"]] = item["index_envelope"]
        mapping[item["object_body"]] = item["evidence_envelope"]

    page = reader(
        items,
        mapping,
        limits=ReplayLimits(page_size=2, max_indexes=2),
    ).export(limit=2)

    assert not page.records
    assert not page.quarantines
    assert page.cursor is None
    assert page.complete is False
    assert page.inspected_indexes == 0

def test_generated_large_discovery_stops_at_first_over_limit_index():
    consumed = 0

    class Backend:
        def discover_evidence_indexes(self, *, max_events, page_size, max_pages):
            del max_events, page_size, max_pages

            def references():
                nonlocal consumed
                for index in range(100_000):
                    consumed += 1
                    digest = f"{index:064x}"
                    yield {
                        "key": evidence_index_key(digest),
                        "ciphertext_sha256": digest,
                        "ciphertext_size": 1,
                    }

            return references()

        get_evidence_index_bytes = lambda *_args, **_kwargs: pytest.fail("bounded discovery fetched an index")
        get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("bounded discovery fetched evidence")

    page = ReplayReader(
        Backend(),
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        limits=ReplayLimits(max_indexes=1000),
        decryptor=lambda *_args, **_kwargs: pytest.fail("bounded discovery decrypted an index"),
    ).export(limit=1)

    assert consumed == 1001
    assert page.records == ()
    assert page.quarantines == ()
    assert page.cursor is None
    assert page.complete is False
    assert page.inspected_indexes == 0

def test_cumulative_scan_cap_returns_incomplete_without_fetching_evidence():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    scan_budget = len(item["index_body"]) + len(item["object_body"]) + 2
    backend = FakeBackend([item])
    backend.get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("scan cap fetched evidence")

    page = ReplayReader(
        backend,
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda body, **_kwargs: {item["index_body"]: item["index_envelope"]}[body],
        limits=ReplayLimits(
            max_scan_bytes=scan_budget - 1,
        ),
    ).export(limit=1)

    assert page.records == ()
    assert page.quarantines == ()
    assert page.cursor is None
    assert page.complete is False
    assert page.inspected_indexes == 0

    exact_page = reader(
        [item],
        {
            item["index_body"]: item["index_envelope"],
            item["object_body"]: item["evidence_envelope"],
        },
        limits=ReplayLimits(max_scan_bytes=scan_budget),
    ).export(limit=1)

    assert exact_page.records
    assert exact_page.complete

def test_failed_index_reads_still_consume_cumulative_scan_budget():
    read_keys = []
    refs = []
    for index in range(1, 4):
        digest = f"{index:064x}"
        refs.append({
            "key": evidence_index_key(digest),
            "ciphertext_sha256": digest,
            "ciphertext_size": 5,
        })

    class Backend:
        def discover_evidence_indexes(self, **_kwargs):
            return refs

        def get_evidence_index_bytes(self, key, **_kwargs):
            read_keys.append(key)
            raise OSError("synthetic read failure")

        get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("failed indexes fetched evidence")

    previous = ReplayCursor(
        "profile-personal",
        "private-r2",
        evidence_index_key("0" * 64),
    ).encode()
    page = ReplayReader(
        Backend(),
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda *_args, **_kwargs: pytest.fail("failed index decrypted"),
        limits=ReplayLimits(max_indexes=3, max_scan_bytes=12),
    ).export(cursor=previous, limit=8)

    assert len(read_keys) == 2
    assert page.records == ()
    assert page.quarantines == ()
    assert page.cursor == previous
    assert page.complete is False
    assert page.inspected_indexes == 0

@pytest.mark.parametrize(
    ("listed_size", "expected_reason"),
    [
        ("oversized", "ciphertext-too-large"),
        ("malformed", "ciphertext-size-invalid"),
    ],
)
def test_unselected_invalid_index_blocks_record_emission(listed_size, expected_reason):
    items = []
    mapping = {}
    for event_id in ("event-valid-index", "event-invalid-index"):
        segment = fixture("golden-session-segment.json")
        segment["event_id"] = event_id
        segment["session_id"] = "session-shared"
        segment["asset_refs"] = []
        item = entry(segment, payload=canonical_json(segment))
        items.append(item)
        mapping[item["index_body"]] = item["index_envelope"]
        mapping[item["object_body"]] = item["evidence_envelope"]
    items.sort(key=lambda item: item["index_key"])
    invalid_key = items[-1]["index_key"]
    cursor_after_first = ReplayCursor(
        "profile-personal",
        "private-r2",
        items[0]["index_key"],
    ).encode()

    class Backend(FakeBackend):
        def discover_evidence_indexes(self, *, max_events, page_size, max_pages):
            refs = super().discover_evidence_indexes(
                max_events=max_events,
                page_size=page_size,
                max_pages=max_pages,
            )
            if listed_size == "oversized":
                refs[-1]["ciphertext_size"] = ReplayLimits().max_ciphertext_bytes + 1
            else:
                refs[-1]["ciphertext_size"] = 0
                refs[-1]["listing_error"] = "ciphertext-size-invalid"
            return refs

        def get_evidence_index_bytes(self, key, expected_size=None):
            if key == invalid_key:
                pytest.fail("invalid index body fetched")
            return super().get_evidence_index_bytes(key, expected_size)

    replay = ReplayReader(
        Backend(items),
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda body, **_kwargs: mapping[body],
    )

    page = replay.export(limit=1)
    inspection = replay.inspect(cursor=cursor_after_first, limit=1)

    assert page.records == ()
    assert {item["reason_code"] for item in page.quarantines} == {expected_reason}
    assert page.cursor is None
    assert page.complete is False
    assert page.inspected_indexes == 0
    if listed_size == "malformed":
        assert inspection["indexes"][0]["ciphertext_size"] is None
        assert inspection["indexes"][0]["error_code"] == expected_reason
    else:
        assert inspection["indexes"][0]["ciphertext_size"] == ReplayLimits().max_ciphertext_bytes + 1


def test_corrupt_off_page_index_blocks_complete_record_emission():
    items = []
    mapping = {}
    for event_id in ("event-unreadable-index", "event-selected-index"):
        segment = fixture("golden-session-segment.json")
        segment["event_id"] = event_id
        segment["session_id"] = "session-shared"
        segment["asset_refs"] = []
        item = entry(segment, payload=canonical_json(segment))
        items.append(item)
        mapping[item["index_body"]] = item["index_envelope"]
        mapping[item["object_body"]] = item["evidence_envelope"]
    items.sort(key=lambda item: item["index_key"])
    unreadable_key = items[0]["index_key"]
    previous = ReplayCursor(
        "profile-personal",
        "private-r2",
        unreadable_key,
    ).encode()

    class Backend(FakeBackend):
        def get_evidence_index_bytes(self, key, expected_size=None):
            if key == unreadable_key:
                raise OSError("synthetic unreadable index")
            return super().get_evidence_index_bytes(key, expected_size)

    page = ReplayReader(
        Backend(items),
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda body, **_kwargs: mapping[body],
    ).export(cursor=previous, limit=1)

    assert page.records == ()
    assert {item["reason_code"] for item in page.quarantines} == {"corrupt-ciphertext"}
    assert page.cursor == previous
    assert page.complete is False
    assert page.inspected_indexes == 0

@pytest.mark.parametrize("local_only_at", ["index", "evidence"])
def test_local_only_evidence_is_not_exported_from_private_r2(local_only_at):
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    if local_only_at == "evidence":
        segment["capture"]["policy_decision"] = "local-only"
    item = entry(segment, payload=canonical_json(segment))
    if local_only_at == "index":
        item["index_envelope"]["manifest"]["policy"]["decision"] = "local-only"
    mapping = {
        item["index_body"]: item["index_envelope"],
        item["object_body"]: item["evidence_envelope"],
    }

    page = reader([item], mapping).export(limit=1)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} == {"policy-mismatch"}


def test_final_without_last_segment_digest_quarantines_its_session():
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    final = fixture("golden-session-final.json")
    segment["session_id"] = "session-final-link"
    final["session_id"] = segment["session_id"]
    final.pop("last_segment_sha256", None)
    segment_entry = entry(segment, payload=canonical_json(segment))
    final_entry = entry(final, payload=canonical_json(final))
    entries = [segment_entry, final_entry]
    mapping = {
        item["index_body"]: item["index_envelope"]
        for item in entries
    } | {
        item["object_body"]: item["evidence_envelope"]
        for item in entries
    }

    page = reader(entries, mapping).export(limit=8)

    assert not page.records
    assert "broken-chain" in {receipt["reason_code"] for receipt in page.quarantines}


def test_malformed_index_reference_keeps_export_incomplete():
    class Backend:
        discover_evidence_indexes = lambda *_args, **_kwargs: [{"key": "not-an-index"}]
        get_evidence_index_bytes = lambda *_args, **_kwargs: pytest.fail("malformed index fetched")
        get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("evidence fetched")

    replay_reader = ReplayReader(
        Backend(),
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda *_args, **_kwargs: pytest.fail("malformed index decrypted"),
    )

    page = replay_reader.export(limit=1)

    assert not page.records
    assert page.complete is False
    assert page.cursor is None
def test_incomplete_r2_discovery_preserves_consumer_cursor():
    class Backend:
        def discover_evidence_indexes(self, **_kwargs):
            raise R2EvidenceError("index-discovery-incomplete")

        get_evidence_index_bytes = lambda *_args, **_kwargs: pytest.fail("incomplete discovery fetched an index")
        get_evidence_bytes = lambda *_args, **_kwargs: pytest.fail("incomplete discovery fetched evidence")

    previous = ReplayCursor(
        "profile-personal",
        "private-r2",
        evidence_index_key("a" * 64),
    ).encode()
    page = ReplayReader(
        Backend(),
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        decryptor=lambda *_args, **_kwargs: pytest.fail("incomplete discovery decrypted an index"),
    ).export(cursor=previous, limit=1)

    assert page.records == ()
    assert page.quarantines == ()
    assert page.cursor == previous
    assert page.complete is False
    assert page.inspected_indexes == 0

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
    unknown["index_body"] = b"index:unknown"
    unknown["index_key"] = evidence_index_key(hashlib.sha256(unknown["index_body"]).hexdigest())
    bad = dict(item)
    bad["index_body"] = b"changed"
    bad["index_key"] = evidence_index_key(hashlib.sha256(b"other").hexdigest())
    mapping = {
        unknown["index_body"]: unknown["index_envelope"],
        bad["index_body"]: item["index_envelope"],
        item["object_body"]: item["evidence_envelope"],
    }
    page = reader([unknown, bad], mapping).export(limit=2)
    assert {item["reason_code"] for item in page.quarantines} >= {"unknown_major", "digest-mismatch"}


def test_production_crypto_unknown_schema_reason_is_preserved(monkeypatch):
    segment = fixture("golden-session-segment.json")
    segment["asset_refs"] = []
    item = entry(segment, payload=canonical_json(segment))
    backend = FakeBackend([item])

    def decrypt_unknown_schema(*_args, **_kwargs):
        raise CryptoError(CryptoErrorCode.UNKNOWN_SCHEMA)

    monkeypatch.setattr("josh_room.pcc_replay.decrypt_envelope", decrypt_unknown_schema)
    page = ReplayReader(
        backend,
        profile_id="profile-personal",
        destination="private-r2",
        workspace_id="workspace-synthetic",
        identity_paths=(Path("synthetic-identity"),),
    ).export(limit=1)

    assert not page.records
    assert {receipt["reason_code"] for receipt in page.quarantines} == {"unknown_major"}

def test_identity_required_without_test_decryptor():
    class Backend:
        get_evidence_index_bytes = lambda *_args, **_kwargs: b""
        get_evidence_bytes = lambda *_args, **_kwargs: b""
        discover_evidence_indexes = lambda *_args, **_kwargs: []

    with pytest.raises(TypeError, match="workspace_id"):
        ReplayReader(Backend(), profile_id="profile-personal", destination="private-r2")
    with pytest.raises(ReplayError, match="identity-unavailable"):
        ReplayReader(
            Backend(),
            profile_id="profile-personal",
            destination="private-r2",
            workspace_id="workspace-synthetic",
        )._decrypt(b"x", expected_kind="index-event")
