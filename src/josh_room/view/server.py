"""Localhost-only, read-only Room View HTTP server with DNS-rebinding defense."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from urllib.parse import parse_qs, unquote, urlsplit

from .contract import (
    Attachment,
    NodeState,
    StaleStatus,
    ViewDescriptor,
    ViewState,
    ViewStateProvider,
    ViewValidationError,
    validate_attachment_name,
    validate_safe_label,
    validate_sha256,
)
from .exporter import (
    DEFAULT_MAX_SIZE_BYTES,
    ExportSizeLimitError,
    SensitivityAckRequiredError,
    build_offline_export,
    inject_before_last_body,
    load_viewer_template,
)
from .room_provider import RoomWorkspaceProvider
from .sanitizer import (
    DEFAULT_SANITIZATION_POLICY,
    SanitizationPolicy,
    SensitivityAssessment,
    resolve_sensitivity,
    sanitize_descriptor,
    sanitize_node,
    sanitize_state,
)

LIVE_VIEWER_CSP = (
    "default-src 'none'; "
    "connect-src 'self'; "
    "script-src 'unsafe-inline' blob: 'wasm-unsafe-eval'; "
    "worker-src blob:; "
    "style-src 'unsafe-inline'; "
    "img-src 'self' blob: data:; "
    "font-src data:; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ViewCollector:
    """Background polling collector that sanitizes provider state under a lock."""

    def __init__(
        self,
        provider: ViewStateProvider,
        *,
        interval_seconds: float = 5.0,
        workspace: Path | None = None,
        policy: SanitizationPolicy = DEFAULT_SANITIZATION_POLICY,
        scoped_live: bool = False,
    ) -> None:
        self._scoped_live = scoped_live and isinstance(provider, RoomWorkspaceProvider)
        self._observe = provider.overview if self._scoped_live else provider.state
        self._provider = provider
        self._interval = max(0.25, float(interval_seconds))
        self._workspace = workspace
        self._policy = policy
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._descriptor: ViewDescriptor = sanitize_descriptor(provider.describe(), policy=policy)
        self._sensitivity: SensitivityAssessment = resolve_sensitivity(
            self._descriptor, workspace=workspace
        )
        self._last_ok: str | None = None
        self._state: ViewState = self._empty_stale_state("initializing")
        self.poll_once()

    def _empty_stale_state(self, reason: str) -> ViewState:
        now = _utc_now_iso()
        return ViewState(
            taken_at=now,
            since=now,
            groups=(),
            items=(),
            steps=(),
            summary={},
            stale=StaleStatus(reason=reason, last_ok=self._last_ok),
        )

    @property
    def provider(self) -> ViewStateProvider:
        return self._provider

    @property
    def descriptor(self) -> ViewDescriptor:
        return self._descriptor

    @property
    def sensitivity(self) -> SensitivityAssessment:
        return self._sensitivity

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def poll_once(self) -> None:
        """Fetch and sanitize one state snapshot; failures never crash or leak raw text."""
        extra_sanitizer = getattr(self._provider, "sanitize", None)
        if not callable(extra_sanitizer):
            extra_sanitizer = None
        try:
            raw_state = self._observe()
            sanitized = sanitize_state(
                self._descriptor,
                raw_state,
                policy=self._policy,
                extra_sanitizer=extra_sanitizer,
            )
        except ViewValidationError as error:
            with self._lock:
                if error.code == "workspace-binding-changed":
                    self._state = self._empty_stale_state("workspace-binding-changed")
                else:
                    self._mark_stale_locked("sanitization-rejected")
            return
        except Exception:  # noqa: BLE001 - provider errors become safe stale markers
            with self._lock:
                self._mark_stale_locked("collector-error")
            return

        with self._lock:
            self._last_ok = sanitized.taken_at
            self._state = sanitized
            self._clear_invalid_binding_locked()

    def _clear_invalid_binding_locked(self) -> None:
        if getattr(self._provider, "binding_invalidated", False) is True:
            self._state = self._empty_stale_state("workspace-binding-changed")

    def _mark_stale_locked(self, reason: str) -> None:
        current = self._state
        stale = StaleStatus(reason=reason, last_ok=self._last_ok)
        self._state = ViewState(
            taken_at=current.taken_at,
            since=current.since,
            groups=current.groups,
            items=current.items,
            nodes=current.nodes,
            steps=current.steps,
            summary=current.summary,
            stale=stale,
        )

    def lookup_preview(self, object_id: str, slot: str) -> dict[str, object] | None:
        """Return an already-sanitized read-only preview slot from the active snapshot."""
        with self._lock:
            self._clear_invalid_binding_locked()
            for node in self._state.nodes:
                if node.id == object_id:
                    for prev in node.previews:
                        if prev.slot == slot:
                            return prev.to_dict()
            for item in self._state.items:
                if item.id == object_id:
                    for prev in item.previews:
                        if prev.slot == slot:
                            return prev.to_dict()
        return None

    def current_payload(self) -> dict[str, object]:
        with self._lock:
            self._clear_invalid_binding_locked()
            return {
                "descriptor": self._descriptor.to_dict(),
                "state": self._state.to_dict(),
                "sensitivity": self._sensitivity.to_dict(),
                "server": {
                    "mode": "live",
                    **({"filesystem": True} if self._scoped_live else {}),
                    "poll_interval_s": self._interval,
                },
            }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="josh-room-view-collector", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval):
            self.poll_once()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


class _SnapshotProviderAdapter:
    """Wraps a collector's current sanitized snapshot for consistent live /export.html."""

    def __init__(self, collector: ViewCollector) -> None:
        self._collector = collector
        with collector._lock:
            collector._clear_invalid_binding_locked()
            self._descriptor = collector._descriptor
            self._state = collector._state

    def describe(self) -> ViewDescriptor:
        return self._descriptor

    def state(self) -> ViewState:
        self.assert_binding()
        if self._collector._scoped_live:
            return self._collector.provider.state()
        return self._state

    def verify_binding(self) -> None:
        self.assert_binding()

    def assert_binding(self) -> None:
        verify = getattr(self._collector.provider, "verify_binding", None)
        if callable(verify):
            verify()
        if getattr(self._collector.provider, "binding_invalidated", False) is True:
            raise ViewValidationError("workspace-binding-changed", "Room workspace binding changed; launch Room View again")

    def attachment(self, item_id: str, name: str):
        result = self._collector.provider.attachment(item_id, name)
        self.assert_binding()
        return result


_FS_PARAMS = {
    "/fs/list": {"id", "generation", "cursor"},
    "/fs/inspect": {"id", "generation"},
    "/fs/preview": {"id", "generation"},
    "/fs/attachment": {"id", "generation", "name", "digest"},
}
_FS_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_FS_MAX_BYTES = 1024 * 1024


def _filesystem_result(adapter, path: str, query: dict[str, list[str]], policy: SanitizationPolicy):
    """Accept only opaque capabilities; apply the same node sanitizer used for exports."""
    values = {key: items[0] for key, items in query.items() if key != "token"}
    if values.keys() - _FS_PARAMS[path] or any(len(items) != 1 for items in query.values()):
        raise ViewValidationError("invalid-query", "invalid filesystem parameters")
    object_id = validate_safe_label(values.get("id"), "object id")
    generation = values.get("generation")
    cursor = values.get("cursor")
    bootstrap = path == "/fs/list" and object_id == "root" and cursor is None
    if generation is None and not bootstrap:
        raise ViewValidationError("invalid-query", "generation is required")
    if any(not _FS_TOKEN.fullmatch(value) for value in (generation, cursor) if value is not None):
        raise ViewValidationError("invalid-query", "invalid generation or cursor")
    if path == "/fs/attachment":
        name = validate_attachment_name(values.get("name"))
        digest = validate_sha256(values.get("digest"))
        attachment = adapter.attachment(object_id, generation, name, digest)
        if (attachment is None or attachment.sha256 != digest or attachment.name != name
                or attachment.media_type not in {"application/pdf", "image/png", "image/webp", "image/jpeg"}):
            raise ViewValidationError("filesystem-unknown", "attachment unavailable")
        if attachment.size > _FS_MAX_BYTES:
            raise ViewValidationError("response-too-large", "attachment too large")
        return attachment
    if path == "/fs/list":
        result = adapter.listing(object_id, generation, cursor)
    elif path == "/fs/inspect":
        result = adapter.inspect(object_id, generation)
    else:
        result = adapter.preview(object_id, generation)

    def clean(raw):
        return sanitize_node(NodeState.from_dict(raw), policy).to_dict()

    payload = dict(result)
    for key in ("directory", "node"):
        if key in result:
            payload[key] = clean(result[key])
    for key in ("entries", "ancestors"):
        if key in result:
            payload[key] = [clean(node) for node in result[key]]
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _FS_MAX_BYTES:
        raise ViewValidationError("response-too-large", "directory page too large")
    return encoded


class RoomViewServer:
    """Stdlib ThreadingHTTPServer bound strictly to 127.0.0.1 with read-only GET routes."""

    def __init__(
        self,
        provider: ViewStateProvider,
        *,
        port: int = 0,
        interval_seconds: float = 5.0,
        workspace: Path | None = None,
        require_token: bool = False,
        max_export_bytes: int = DEFAULT_MAX_SIZE_BYTES,
        template_path: Path | None = None,
        policy: SanitizationPolicy = DEFAULT_SANITIZATION_POLICY,
    ) -> None:
        self.collector = ViewCollector(
            provider,
            interval_seconds=interval_seconds,
            workspace=workspace,
            policy=policy,
            scoped_live=True,
        )
        self._workspace = workspace
        self._max_export_bytes = max_export_bytes
        self._template_path = template_path
        self._policy = policy
        self._token = secrets.token_urlsafe(24) if require_token else None
        self._base_template = load_viewer_template(template_path)

        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                # Quiet logging; never emit state payloads, queries, or tokens
                return

            def _allowed_hosts(self) -> set[str]:
                actual_port = owner.port
                return {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}

            def _check_host(self) -> bool:
                if len(self.headers.get_all("Host", [])) != 1:
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden-host-header"},
                                    head_only=self.command == "HEAD")
                    return False
                host = (self.headers.get("Host") or "").strip().lower()
                forwarded_loopback = False
                if owner._token:
                    try:
                        authority = urlsplit(f"http://{host}")
                        forwarded_loopback = (
                            authority.hostname in {"localhost", "127.0.0.1", "::1"}
                            and authority.port is not None and 0 < authority.port <= 65535
                            and not authority.username and not authority.password
                            and not authority.path and not authority.query and not authority.fragment
                        )
                    except ValueError:
                        pass
                if host not in self._allowed_hosts() and not forwarded_loopback:
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {"ok": False, "error": "forbidden-host-header"},
                    )
                    return False
                return True

            def _check_token(self, query: dict[str, list[str]]) -> bool:
                if owner._token is None:
                    return True
                header_tok = (self.headers.get("X-Josh-Room-View-Token") or "").strip()
                query_tok = (query.get("token", [""])[0] or "").strip()
                candidate = header_tok or query_tok
                if not candidate.isascii() or not hmac.compare_digest(candidate, owner._token):
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {"ok": False, "error": "invalid-session-token"},
                    )
                    return False
                return True

            def _base_headers(self, content_type: str, length: int, *, extra: dict[str, str] | None = None) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Cache-Control", "no-store")
                if extra:
                    for key, value in extra.items():
                        self.send_header(key, value)

            def _send_bytes(
                self,
                status: HTTPStatus,
                body: bytes,
                content_type: str,
                *,
                extra_headers: dict[str, str] | None = None,
                head_only: bool = False,
            ) -> None:
                self.send_response(status)
                self._base_headers(content_type, len(body), extra=extra_headers)
                self.end_headers()
                if not head_only and self.command != "HEAD":
                    self.wfile.write(body)

            def _send_json(
                self,
                status: HTTPStatus,
                payload: dict[str, object],
                *,
                extra_headers: dict[str, str] | None = None,
                head_only: bool = False,
            ) -> None:
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                self._send_bytes(
                    status,
                    encoded,
                    "application/json; charset=utf-8",
                    extra_headers=extra_headers,
                    head_only=head_only,
                )

            def _reject_method(self) -> None:
                if not self._check_host():
                    return
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"ok": False, "error": "method-not-allowed"},
                    extra_headers={"Allow": "GET, HEAD"},
                )

            do_POST = _reject_method
            do_PUT = _reject_method
            do_DELETE = _reject_method
            do_PATCH = _reject_method
            do_OPTIONS = _reject_method

            def do_HEAD(self) -> None:
                self._handle_get(head_only=True)

            def do_GET(self) -> None:
                self._handle_get(head_only=False)

            def _filesystem(self, path: str, query: dict[str, list[str]], *, head_only: bool) -> None:
                def error(status: HTTPStatus, code: str) -> None:
                    self._send_json(status, {"ok": False, "error": code}, head_only=head_only)

                if owner._filesystem is None or path not in _FS_PARAMS:
                    error(HTTPStatus.NOT_FOUND, "not-found")
                    return
                if not owner._fs_lock.acquire(blocking=False):
                    error(HTTPStatus.TOO_MANY_REQUESTS, "filesystem-busy")
                    return
                try:
                    try:
                        if owner._filesystem is None:
                            raise ViewValidationError("filesystem-stale", "session closed")
                        result = _filesystem_result(owner._filesystem, path, query, owner._policy)
                        owner.collector.provider.verify_binding()
                    finally:
                        owner._fs_lock.release()
                except ViewValidationError as exc:
                    public_errors = {
                        "filesystem-stale": HTTPStatus.CONFLICT,
                        "workspace-binding-changed": HTTPStatus.CONFLICT,
                        "filesystem-unknown": HTTPStatus.NOT_FOUND,
                        "response-too-large": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    }
                    error(public_errors.get(exc.code, HTTPStatus.BAD_REQUEST),
                          exc.code if exc.code in public_errors else "invalid-filesystem-query")
                    return
                except Exception:  # noqa: BLE001 - never expose filesystem exception text over HTTP
                    error(HTTPStatus.SERVICE_UNAVAILABLE, "filesystem-unavailable")
                    return
                if isinstance(result, Attachment):
                    self._send_bytes(HTTPStatus.OK, result.data, result.media_type,
                                     extra_headers={"Content-Disposition": f'inline; filename="{result.name}"',
                                                    "Content-Security-Policy": "sandbox"},
                                     head_only=head_only)
                else:
                    self._send_bytes(HTTPStatus.OK, result, "application/json; charset=utf-8", head_only=head_only)

            def _handle_get(self, *, head_only: bool) -> None:
                if not self._check_host():
                    return
                origin = self.headers.get_all("Origin", [])
                if origin and origin != [f"http://{self.headers['Host']}"]:
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden-origin"}, head_only=head_only)
                    return
                try:
                    parsed = urlsplit(self.path)
                    query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=12)
                except ValueError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid-query"}, head_only=head_only)
                    return
                if not self._check_token(query):
                    return
                path = parsed.path

                if path.startswith("/fs/"):
                    if len(self.path) > 2048 or self.headers.get("Sec-Fetch-Site") == "cross-site":
                        self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden-request"}, head_only=head_only)
                        return
                    self._filesystem(path, query, head_only=head_only)
                    return

                if path in {"/", "/index.html"}:
                    live_cfg = {
                        "mode": "live",
                        "pollIntervalMs": int(owner.collector.interval_seconds * 1000),
                        "token": owner._token,
                    }
                    cfg_tag = (
                        '<script id="room-view-live-config" type="application/json">'
                        + json.dumps(live_cfg, separators=(",", ":"))
                        + "</script>"
                    )
                    # Replace offline connect-src 'none' meta with live connect-src 'self'
                    html = owner._base_template.replace("connect-src 'none'", "connect-src 'self'", 1)
                    html = inject_before_last_body(html, cfg_tag)
                    self._send_bytes(
                        HTTPStatus.OK,
                        html.encode("utf-8"),
                        "text/html; charset=utf-8",
                        extra_headers={"Content-Security-Policy": LIVE_VIEWER_CSP},
                        head_only=head_only,
                    )
                    return

                if path == "/state.json":
                    self._send_json(
                        HTTPStatus.OK,
                        owner.collector.current_payload(),
                        head_only=head_only,
                    )
                    return

                if path in {"/export.html", "/export-preview.json"}:
                    if not owner._export_lock.acquire(blocking=False):
                        self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "error": "export-busy"}, head_only=head_only)
                        return
                    try:
                        self._export(path, query, head_only=head_only)
                    finally:
                        owner._export_lock.release()
                    return

                if owner.collector._scoped_live and path.startswith(("/content/", "/attachments/")):
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not-found"}, head_only=head_only)
                    return

                if path.startswith("/content/"):
                    rest = path[len("/content/"):]
                    parts = rest.split("/")
                    if len(parts) != 2 or not parts[0] or not parts[1]:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "invalid-content-route"},
                            head_only=head_only,
                        )
                        return
                    raw_obj, raw_slot = unquote(parts[0]), unquote(parts[1])
                    try:
                        obj_id = validate_safe_label(raw_obj, "object id")
                        slot_id = validate_safe_label(raw_slot, "preview slot")
                    except ViewValidationError:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "invalid-content-identifier"},
                            head_only=head_only,
                        )
                        return
                    preview = owner.collector.lookup_preview(obj_id, slot_id)
                    if preview is None:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"ok": False, "error": "content-not-found"},
                            head_only=head_only,
                        )
                        return
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "object_id": obj_id, "preview": preview},
                        head_only=head_only,
                    )
                    return

                if path.startswith("/attachments/"):
                    rest = path[len("/attachments/"):]
                    parts = rest.split("/")
                    if len(parts) != 2 or not parts[0] or not parts[1]:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "invalid-attachment-route"},
                            head_only=head_only,
                        )
                        return
                    raw_item, raw_name = unquote(parts[0]), unquote(parts[1])
                    try:
                        item_id = validate_safe_label(raw_item, "item id")
                        att_name = validate_attachment_name(raw_name)
                    except ViewValidationError:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "invalid-attachment-identifier"},
                            head_only=head_only,
                        )
                        return
                    att = owner.collector.provider.attachment(item_id, att_name)
                    if att is None:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {"ok": False, "error": "attachment-not-found"},
                            head_only=head_only,
                        )
                        return
                    extra = {
                        "Content-Disposition": f'inline; filename="{att.name}"',
                    }
                    if att.media_type == "text/html":
                        # Never execute HTML attachments if opened directly
                        extra["Content-Security-Policy"] = "sandbox"
                    self._send_bytes(
                        HTTPStatus.OK,
                        att.data,
                        att.media_type,
                        extra_headers=extra,
                        head_only=head_only,
                    )
                    return

                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not-found"},
                    head_only=head_only,
                )

            def _export(self, path: str, query: dict[str, list[str]], *, head_only: bool) -> None:
                if path == "/export-preview.json":
                    include_att = query.get("no_attachments", ["0"])[0] not in {"1", "true"}
                    snapshot_provider = _SnapshotProviderAdapter(owner.collector)
                    try:
                        artifact = build_offline_export(
                            snapshot_provider,
                            include_attachments=include_att,
                            max_size_bytes=None,
                            sensitivity_ack=True,
                            workspace=owner._workspace,
                            template_path=owner._template_path,
                            policy=owner._policy,
                        )
                        snapshot_provider.assert_binding()
                    except (ValueError, OSError):
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"ok": False, "error": "export-preview-failed"},
                            head_only=head_only,
                        )
                        return
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "sensitivity": artifact.sensitivity.to_dict(),
                            "accounting": artifact.accounting,
                            "max_size_bytes": owner._max_export_bytes,
                            "exceeds_max_size": len(artifact.html_bytes) > owner._max_export_bytes,
                        },
                        head_only=head_only,
                    )
                    return

                if path == "/export.html":
                    include_att = query.get("no_attachments", ["0"])[0] not in {"1", "true"}
                    ack = query.get("ack", ["0"])[0] in {"1", "true", "yes"}
                    snapshot_provider = _SnapshotProviderAdapter(owner.collector)
                    try:
                        artifact = build_offline_export(
                            snapshot_provider,
                            include_attachments=include_att,
                            max_size_bytes=owner._max_export_bytes,
                            sensitivity_ack=ack,
                            workspace=owner._workspace,
                            template_path=owner._template_path,
                            policy=owner._policy,
                        )
                        snapshot_provider.assert_binding()
                    except (SensitivityAckRequiredError, ExportSizeLimitError) as error:
                        try:
                            snapshot_provider.assert_binding()
                        except (ValueError, OSError):
                            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "export-failed"}, head_only=head_only)
                            return
                        status = HTTPStatus.CONFLICT if isinstance(error, SensitivityAckRequiredError) else HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                        self._send_json(status, error.result, head_only=head_only)
                        return
                    except (ValueError, OSError):
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"ok": False, "error": "export-failed"},
                            head_only=head_only,
                        )
                        return
                    disposition = (
                        'attachment; filename="room-view-snapshot.html"'
                        if "download" in query
                        else 'inline; filename="room-view-snapshot.html"'
                    )
                    self._send_bytes(
                        HTTPStatus.OK,
                        artifact.html_bytes,
                        "text/html; charset=utf-8",
                        extra_headers={"Content-Disposition": disposition},
                        head_only=head_only,
                    )
                    return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        try:
            self._filesystem = provider.open_filesystem() if self.collector._scoped_live else None
        except BaseException:
            self._httpd.server_close()
            raise
        self._fs_lock = threading.Lock()
        self._export_lock = threading.Lock()
        self._httpd.daemon_threads = True
        self._serve_thread: threading.Thread | None = None

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def url(self) -> str:
        base = f"http://127.0.0.1:{self.port}/"
        if self._token:
            return f"{base}?token={self._token}"
        return base

    def start_background(self) -> None:
        self.collector.start()
        self._serve_thread = threading.Thread(
            target=self._httpd.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="josh-room-view-http",
            daemon=True,
        )
        self._serve_thread.start()

    def serve_forever(self) -> None:
        self.collector.start()
        try:
            self._httpd.serve_forever(poll_interval=0.25)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self.collector.stop()
        self._httpd.shutdown()
        self._httpd.server_close()
        with self._fs_lock:
            if self._filesystem is not None:
                self._filesystem.close()
                self._filesystem = None
        if self._serve_thread is not None:
            self._serve_thread.join(timeout=2.0)

    def __enter__(self) -> Self:
        self.start_background()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()


def serve_forever_cli(
    provider: ViewStateProvider,
    *,
    port: int = 0,
    interval: float = 5.0,
    workspace: Path | None = None,
    require_token: bool = False,
    json_mode: bool = False,
    max_seconds: float | None = None,
    stop_on_stdin_eof: bool = False,
) -> dict[str, object]:
    """Run the localhost Room View server until interrupted (or max_seconds)."""
    server = RoomViewServer(
        provider,
        port=port,
        interval_seconds=interval,
        workspace=workspace,
        require_token=require_token,
    )
    info = {
        "ok": True,
        "command": "view-serve",
        "url": server.url,
        "host": server.host,
        "port": server.port,
        "interval": interval,
        "read_only": True,
        "sensitivity": server.collector.sensitivity.to_dict(),
    }
    if json_mode:
        print(json.dumps(info, sort_keys=True), flush=True)
    else:
        print(f"Room View serving (read-only, localhost) at {server.url}", flush=True)
    if stop_on_stdin_eof:
        # The extension owns the pipe writer. EOF also covers a crashed owner,
        # without trusting the RCC leader's lifetime or leaving an idle server.
        owner_closed = threading.Event()

        def wait_for_owner() -> None:
            try:
                while sys.stdin.buffer.read(1):
                    pass
            except (OSError, ValueError):
                pass
            finally:
                owner_closed.set()

        server.start_background()
        threading.Thread(target=wait_for_owner, name="room-view-owner", daemon=True).start()
        try:
            owner_closed.wait(max_seconds)
        finally:
            server.shutdown()
        return info
    if max_seconds is not None:
        server.start_background()
        try:
            time.sleep(max(0.0, max_seconds))
        finally:
            server.shutdown()
        return info
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return info
