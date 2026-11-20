"""Focused tests for Josh Room View (issue #202): contract, sanitizer, server, exporter, inspector, CLI."""

from __future__ import annotations

import http.client
import json
import sqlite3
import time
from types import MappingProxyType

import pytest

from josh_room import cli
from josh_room.view import (
    Attachment,
    AttachmentRef,
    GroupState,
    ItemState,
    LegendEntry,
    MetricDescriptor,
    SqlPreset,
    StageDescriptor,
    StepState,
    SyntheticPipelineProvider,
    ViewDescriptor,
    ViewState,
    ViewValidationError,
    resolve_sensitivity,
    sanitize_descriptor,
    sanitize_state,
)
from josh_room.view.exporter import (
    ExportSizeLimitError,
    FromStateProvider,
    SensitivityAckRequiredError,
    build_offline_export,
    export_view_snapshot,
    parse_size_bytes,
)
from josh_room.view.inspector import inspect_view_snapshot
from josh_room.view.server import RoomViewServer
from scripts.build_view_template import DEFAULT_OUTPUT as TEMPLATE_OUTPUT
from scripts.build_view_template import build_template_html


def _minimal_descriptor(**overrides) -> ViewDescriptor:
    base = {
        "title": "Synthetic Unit Pipeline",
        "stages": (
            StageDescriptor(id="ingest", name="Ingest", order=0),
            StageDescriptor(id="verify", name="Verify", order=1, terminal=True),
        ),
        "group_label": "Sector",
        "legend": {
            "pass": LegendEntry(label="Pass", color="#3FB68B", shape="solid"),
            "fail": LegendEntry(label="Fail", color="#F0673A", shape="notch"),
            "skip": LegendEntry(label="Skip", color="#8A93A6", shape="hollow"),
        },
        "metric": MetricDescriptor(name="duration", min=0.0, max=1000.0, unit="ms"),
        "sensitivity": "synthetic",
    }
    base.update(overrides)
    return ViewDescriptor(**base)


def _minimal_state(**overrides) -> ViewState:
    base = {
        "taken_at": "2026-11-20T15:42:00Z",
        "since": "2026-11-20T15:00:00Z",
        "groups": (GroupState(id="sector-alpha", name="Sector Alpha", order=0),),
        "items": (
            ItemState(
                id="item-0001",
                group="sector-alpha",
                stage="verify",
                kind="pass",
                label="verify-01.pass",
                updated_at="2026-11-20T15:41:00Z",
                metric=42.5,
                fields={"suite": "synthetic-alpha"},
            ),
        ),
        "steps": (StepState(item_id="item-0001", name="ingest", at="2026-11-20T15:40:00Z"),),
        "summary": {"total_items": 1},
    }
    base.update(overrides)
    return ViewState(**base)


class _SimpleProvider:
    def __init__(self, descriptor: ViewDescriptor, state: ViewState, attachments: dict[tuple[str, str], Attachment] | None = None):
        self._descriptor = descriptor
        self._state = state
        self._attachments = attachments or {}

    def describe(self) -> ViewDescriptor:
        return self._descriptor

    def state(self) -> ViewState:
        return self._state

    def attachment(self, item_id: str, name: str) -> Attachment | None:
        return self._attachments.get((item_id, name))


# --- 1. Contract & Validation ---


@pytest.mark.parametrize("bad_id", ["", "../escape", "bad id", "a/b", "-leading", "x" * 65])
def test_contract_rejects_bad_item_and_stage_ids(bad_id):
    with pytest.raises(ViewValidationError):
        StageDescriptor(id=bad_id, name="Stage", order=0)
    with pytest.raises(ViewValidationError):
        ItemState(
            id=bad_id,
            group="sector-alpha",
            stage="ingest",
            kind="pass",
            label="ok",
            updated_at="2026-11-20T15:41:00Z",
        )


@pytest.mark.parametrize("bad_name", ["../secret.pdf", "a/b.pdf", ".hidden", "has space.pdf", "a..b.pdf"])
def test_contract_rejects_bad_attachment_names(bad_name):
    with pytest.raises(ViewValidationError):
        AttachmentRef(name=bad_name, media_type="application/pdf", size=10, digest="a" * 64)


def test_contract_rejects_invalid_media_type_and_bad_sql_preset():
    with pytest.raises(ViewValidationError):
        AttachmentRef(name="doc.pdf", media_type="not a media type", size=10, digest="a" * 64)
    with pytest.raises(ViewValidationError):
        SqlPreset(id="bad", name="Bad", sql="DROP TABLE items")
    with pytest.raises(ViewValidationError):
        SqlPreset(id="bad2", name="Bad2", sql="SELECT * FROM items; DELETE FROM items")


def test_contract_rejects_broken_referential_integrity():
    desc = _minimal_descriptor()
    bad_group_state = _minimal_state(
        items=(
            ItemState(
                id="item-0001",
                group="missing-group",
                stage="verify",
                kind="pass",
                label="broken",
                updated_at="2026-11-20T15:41:00Z",
            ),
        )
    )
    with pytest.raises(ViewValidationError, match="unknown group"):
        sanitize_state(desc, bad_group_state)


# --- 2. Sanitizer & Sensitivity ---


def test_sanitizer_rejects_oversized_text_long_digits_secrets_and_host_paths():
    desc = _minimal_descriptor()

    # Oversized free text
    oversized = _minimal_state(
        items=(
            ItemState(
                id="item-0001",
                group="sector-alpha",
                stage="verify",
                kind="pass",
                label="ok",
                updated_at="2026-11-20T15:41:00Z",
                fields={"note": "x" * 200},
            ),
        )
    )
    with pytest.raises(ViewValidationError) as exc:
        sanitize_state(desc, oversized)
    assert exc.value.code == "text-oversize"

    # Long digit run (>8 digits)
    digits = _minimal_state(
        items=(
            ItemState(
                id="item-0001",
                group="sector-alpha",
                stage="verify",
                kind="pass",
                label="acct-1234567890",
                updated_at="2026-11-20T15:41:00Z",
            ),
        )
    )
    with pytest.raises(ViewValidationError) as exc2:
        sanitize_state(desc, digits)
    assert exc2.value.code == "long-digit-run"

    # Secret-like key
    secret_key = _minimal_state(
        items=(
            ItemState(
                id="item-0001",
                group="sector-alpha",
                stage="verify",
                kind="pass",
                label="ok",
                updated_at="2026-11-20T15:41:00Z",
                fields={"api_token": "synthetic"},
            ),
        )
    )
    with pytest.raises(ViewValidationError) as exc3:
        sanitize_state(desc, secret_key)
    assert exc3.value.code == "forbidden-secret-key"

    # Host path / age key in value
    host_path = _minimal_state(
        items=(
            ItemState(
                id="item-0001",
                group="sector-alpha",
                stage="verify",
                kind="pass",
                label="ok",
                updated_at="2026-11-20T15:41:00Z",
                fields={"location": "/home/synthetic-user/private"},
            ),
        )
    )
    with pytest.raises(ViewValidationError) as exc4:
        sanitize_state(desc, host_path)
    assert exc4.value.code == "forbidden-host-path"


def test_sanitizer_hook_can_tighten_but_never_bypass():
    desc = _minimal_descriptor()
    state = _minimal_state()

    def tighten(st: ViewState) -> ViewState:
        return ViewState(
            taken_at=st.taken_at,
            since=st.since,
            groups=st.groups,
            items=tuple(
                ItemState(
                    id=i.id,
                    group=i.group,
                    stage=i.stage,
                    kind=i.kind,
                    label="redacted-label",
                    updated_at=i.updated_at,
                    metric=i.metric,
                    attachments=i.attachments,
                    fields=MappingProxyType({}),
                )
                for i in st.items
            ),
            steps=st.steps,
            summary=st.summary,
        )

    tightened = sanitize_state(desc, state, extra_sanitizer=tighten)
    assert tightened.items[0].label == "redacted-label"

    def evil_bypass(st: ViewState) -> ViewState:
        return ViewState(
            taken_at=st.taken_at,
            since=st.since,
            groups=st.groups,
            items=tuple(
                ItemState(
                    id=i.id,
                    group=i.group,
                    stage=i.stage,
                    kind=i.kind,
                    label="999999999999",
                    updated_at=i.updated_at,
                )
                for i in st.items
            ),
            steps=st.steps,
            summary=st.summary,
        )

    with pytest.raises(ViewValidationError) as exc:
        sanitize_state(desc, state, extra_sanitizer=evil_bypass)
    assert exc.value.code == "long-digit-run"


def test_sensitivity_takes_most_restrictive_of_provider_and_workspace(tmp_path):
    (tmp_path / ".josh-roomignore").write_text("# josh-room sensitivity: regulated\n", encoding="utf-8")
    desc = _minimal_descriptor(sensitivity="synthetic")
    assessment = resolve_sensitivity(desc, workspace=tmp_path)
    assert assessment.level == "regulated"
    assert assessment.requires_acknowledgment is True
    assert "regulated" in assessment.banner_text


# --- 3. Synthetic Reference Provider ---


def test_synthetic_provider_presets_and_deduplicated_attachments():
    default = SyntheticPipelineProvider(preset="default")
    desc = sanitize_descriptor(default.describe())
    st = sanitize_state(desc, default.state())
    assert len(desc.stages) == 6
    assert len(st.groups) == 3
    assert len(st.items) == 300
    pdf = default.attachment("item-0001", "evidence-report.pdf")
    assert pdf is not None and pdf.data.startswith(b"%PDF-1.4")
    png = default.attachment("item-0001", "before.png")
    assert png is not None and png.data.startswith(b"\x89PNG\r\n\x1a\n")
    html = default.attachment("item-0001", "comparison.html")
    assert html is not None and b"<script>" in html.data

    assert len(SyntheticPipelineProvider(preset="small").state().items) == 10
    assert len(SyntheticPipelineProvider(preset="large").state().items) == 540
    assert SyntheticPipelineProvider(preset="sensitive").describe().sensitivity == "confidential"


# --- 4. Reproducible Template Build ---


def test_checked_in_view_template_is_reproducible_and_has_no_cdn_urls():
    rendered, meta = build_template_html(vendor_dir=None, existing_template=TEMPLATE_OUTPUT)
    assert TEMPLATE_OUTPUT.read_bytes() == rendered.encode("utf-8")
    assert meta["template_version"] == "room-view-template-v1"
    assert set(meta["libraries"]) == {"three", "sqljs", "sqljs-wasm", "pdfjs", "pdfjs-worker"}
    for forbidden in ("unpkg.com", "jsdelivr.net", "cdnjs.", "fonts.googleapis.com", "esm.sh"):
        assert forbidden not in rendered


# --- 5. Export & Inspect ---


def test_export_and_inspect_roundtrip_dedup_disallowed_media_and_limits(tmp_path):
    provider = SyntheticPipelineProvider(preset="default")
    out_file = tmp_path / "snapshot.html"
    report = export_view_snapshot(provider, out_file)
    assert report["ok"] is True
    assert out_file.is_file()
    assert report["accounting"]["item_count"] == 300
    # SHA-256 deduplication verified
    assert report["accounting"]["unique_attachment_blobs"] < report["accounting"]["attachment_count"]
    assert report["accounting"]["attachments_dedup_saved_bytes"] > 0

    inspected = inspect_view_snapshot(out_file)
    assert inspected["ok"] is True
    assert inspected["schema_version"] == "room-view-v1"
    assert inspected["table_counts"]["items"] == 300
    assert inspected["table_counts"]["groups"] == 3
    assert inspected["table_counts"]["stages"] == 6
    assert inspected["external_urls"] == []
    assert "lib-three" in inspected["embedded_libraries"]

    # --no-attachments produces metadata-only snapshot
    no_att_file = tmp_path / "no-att.html"
    no_att_report = export_view_snapshot(provider, no_att_file, include_attachments=False)
    assert no_att_report["accounting"]["unique_attachment_blobs"] == 0
    assert inspect_view_snapshot(no_att_file)["attachments"]["unique_blobs"] == 0

    # --max-size refuses when file exceeds cap
    with pytest.raises(ExportSizeLimitError):
        export_view_snapshot(provider, tmp_path / "too-small.html", max_size_bytes=parse_size_bytes("500KB"))
    assert not (tmp_path / "too-small.html").exists()

    # Sensitivity refuses without explicit --sensitivity-ack
    sens_provider = SyntheticPipelineProvider(preset="sensitive")
    with pytest.raises(SensitivityAckRequiredError) as sens_exc:
        export_view_snapshot(sens_provider, tmp_path / "sens.html", sensitivity_ack=False)
    assert sens_exc.value.preview["item_count"] == 300
    assert not (tmp_path / "sens.html").exists()

    ok_sens = export_view_snapshot(sens_provider, tmp_path / "sens.html", sensitivity_ack=True)
    assert ok_sens["ok"] is True
    assert inspect_view_snapshot(tmp_path / "sens.html")["meta"]["sensitivity"] == "confidential"


def test_export_lists_disallowed_media_type_without_embedding_bytes(tmp_path):
    exe_bytes = b"MZ-synthetic-disallowed-payload"
    att = Attachment(name="tool.bin", media_type="application/octet-stream", data=exe_bytes)
    desc = _minimal_descriptor()
    state = _minimal_state(
        items=(
            ItemState(
                id="item-0001",
                group="sector-alpha",
                stage="verify",
                kind="pass",
                label="item-with-bin",
                updated_at="2026-11-20T15:41:00Z",
                attachments=(AttachmentRef(name=att.name, media_type=att.media_type, size=att.size, digest=att.sha256),),
            ),
        )
    )
    artifact = build_offline_export(_SimpleProvider(desc, state, {("item-0001", "tool.bin"): att}))
    assert artifact.accounting["attachments_skipped_disallowed"] == 1
    assert artifact.accounting["unique_attachment_blobs"] == 0

    db_path = tmp_path / "check.sqlite"
    db_path.write_bytes(artifact.sqlite_bytes)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT name, media_type, bytes FROM attachments").fetchone()
        assert row == ("tool.bin", "application/octet-stream", None)
        assert conn.execute("SELECT COUNT(*) FROM attachment_blobs").fetchone()[0] == 0
    finally:
        conn.close()


def test_from_state_offline_export_and_symlink_rejection(tmp_path):
    provider = SyntheticPipelineProvider(preset="small")
    state_json = tmp_path / "state.json"
    att_dir = tmp_path / "attachments"
    att_dir.mkdir()
    state_json.write_text(
        json.dumps({"descriptor": provider.describe().to_dict(), "state": provider.state().to_dict()}),
        encoding="utf-8",
    )
    # Materialize one attachment for item-0001
    pdf = provider.attachment("item-0001", "evidence-report.pdf")
    assert pdf is not None
    (att_dir / "item-0001").mkdir()
    (att_dir / "item-0001" / "evidence-report.pdf").write_bytes(pdf.data)

    from_state = FromStateProvider(state_json, att_dir)
    out = tmp_path / "from-state.html"
    res = export_view_snapshot(from_state, out)
    assert res["ok"] is True
    assert res["accounting"]["unique_attachment_blobs"] == 1

    # Symlink attachment must be refused
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(pdf.data)
    (att_dir / "item-0001" / "evidence-report.pdf").unlink()
    (att_dir / "item-0001" / "evidence-report.pdf").symlink_to(outside)
    with pytest.raises(ViewValidationError, match="symlink"):
        from_state.attachment("item-0001", "evidence-report.pdf")


# --- 6. Live Localhost Server Security & Behavior ---


def _raw_http(port: int, method: str, path: str, *, host: str | None = None, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        req_headers = dict(headers or {})
        if host is not None:
            req_headers["Host"] = host
        conn.request(method, path, headers=req_headers)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


def test_live_server_host_check_methods_headers_attachments_and_stale_recovery():
    class FlakyProvider(SyntheticPipelineProvider):
        def __init__(self):
            super().__init__(preset="small")
            self.fail_next = False

        def state(self) -> ViewState:
            if self.fail_next:
                raise RuntimeError("SECRET_UPSTREAM_HOST_TRACE /home/private/secret.key")
            return super().state()

    provider = FlakyProvider()
    with RoomViewServer(provider, port=0, interval_seconds=60.0) as server:
        assert server.host == "127.0.0.1"
        assert server.port > 0

        # DNS-rebinding Host header defense
        status, _, _ = _raw_http(server.port, "GET", "/state.json", host=f"evil.example:{server.port}")
        assert status == 403
        status, _, _ = _raw_http(server.port, "GET", "/state.json", host="127.0.0.1:9999")
        assert status == 403

        # Read-only: no POST / PUT / DELETE routes
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, hdrs, _ = _raw_http(server.port, method, "/state.json")
            assert status == 405
            assert "GET" in hdrs.get("Allow", "")

        # Fast /state.json with strict headers
        t0 = time.perf_counter()
        status, hdrs, body = _raw_http(server.port, "GET", "/state.json")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert status == 200
        assert elapsed_ms < 250
        assert hdrs.get("Cache-Control") == "no-store"
        assert hdrs.get("X-Content-Type-Options") == "nosniff"
        assert hdrs.get("Cross-Origin-Resource-Policy") == "same-origin"
        payload = json.loads(body)
        assert len(payload["state"]["items"]) == 10
        assert "stale" not in payload["state"]

        # Valid attachment route + strict headers
        status, hdrs, body = _raw_http(server.port, "GET", "/attachments/item-0001/evidence-report.pdf")
        assert status == 200
        assert body.startswith(b"%PDF-1.4")
        assert hdrs.get("X-Content-Type-Options") == "nosniff"
        assert hdrs.get("Cache-Control") == "no-store"
        assert "inline" in hdrs.get("Content-Disposition", "")

        # HTML attachment gets sandbox CSP header
        status, hdrs, _ = _raw_http(server.port, "GET", "/attachments/item-0001/comparison.html")
        assert status == 200
        assert hdrs.get("Content-Security-Policy") == "sandbox"

        # Invalid attachment routes rejected
        status, _, _ = _raw_http(server.port, "GET", "/attachments/..%2Fetc/passwd")
        assert status == 400
        status, _, _ = _raw_http(server.port, "GET", "/attachments/item-0001/missing.pdf")
        assert status == 404

        # Live root page serves shared viewer with connect-src 'self'
        status, hdrs, html_body = _raw_http(server.port, "GET", "/")
        assert status == 200
        assert "connect-src 'self'" in hdrs.get("Content-Security-Policy", "")
        assert b"room-view-live-config" in html_body

        # Collector failure becomes sanitized stale banner, never crashes or leaks raw exception text
        provider.fail_next = True
        server.collector.poll_once()
        status, _, body = _raw_http(server.port, "GET", "/state.json")
        assert status == 200
        assert b"SECRET_UPSTREAM_HOST_TRACE" not in body
        assert b"/home/private" not in body
        stale_payload = json.loads(body)
        assert stale_payload["state"]["stale"]["reason"] == "collector-error"
        assert stale_payload["state"]["stale"]["last_ok"] == "2026-11-20T15:42:00Z"


def test_live_server_sensitive_export_requires_ack_and_optional_token():
    provider = SyntheticPipelineProvider(preset="sensitive")
    with RoomViewServer(provider, port=0, require_token=True) as server:
        assert server.token is not None
        status, _, _ = _raw_http(server.port, "GET", "/state.json")
        assert status == 403

        tok_hdr = {"X-Josh-Room-View-Token": server.token}
        status, _, _ = _raw_http(server.port, "GET", "/export.html?download=1", headers=tok_hdr)
        assert status == 409

        status, hdrs, body = _raw_http(server.port, "GET", "/export.html?download=1&ack=1", headers=tok_hdr)
        assert status == 200
        assert "attachment" in hdrs.get("Content-Disposition", "")
        assert b'id="room-view-db"' in body


# --- 7. Actual CLI Entrypoint Integration ---


def test_cli_view_commands_are_identity_free_and_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "initialize_system_trust", lambda: None)
    monkeypatch.setattr(cli, "load_runtime_session", lambda **_kwargs: pytest.fail("view must not load R2/runtime session"))
    monkeypatch.setattr(cli, "lookup_keyring_value", lambda *_args: pytest.fail("view must not touch keyring"))
    monkeypatch.delenv("JOSH_ROOM_RESULT_FILE", raising=False)

    out_html = tmp_path / "cli-export.html"
    assert cli.main(["view", "export", "--provider", "synthetic-small", "--output", str(out_html), "--json"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["ok"] is True
    assert exported["accounting"]["item_count"] == 10

    assert cli.main(["view", "inspect", str(out_html), "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["ok"] is True
    assert inspected["table_counts"]["items"] == 10

    # Sensitive CLI export fails closed without --sensitivity-ack
    sens_out = tmp_path / "cli-sens.html"
    assert cli.main(["view", "export", "--provider", "synthetic-sensitive", "--output", str(sens_out), "--json"]) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False
    assert refused["error_code"] == "sensitivity-acknowledgment-required"

    # Quick smoke of actual `view serve` entrypoint
    assert cli.main(["view", "serve", "--provider", "synthetic-small", "--max-seconds", "0.05", "--json"]) == 0
    served = json.loads(capsys.readouterr().out)
    assert served["ok"] is True
    assert served["host"] == "127.0.0.1"
    assert served["read_only"] is True
