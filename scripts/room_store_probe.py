"""Throwaway local acceptance probe for restic-backed Room Store feasibility.

This deliberately does not touch Josh Room catalogs, credentials, or providers.
It creates a synthetic repository and disposable workspace under a temporary
directory. The default execution includes restic work; tests only exercise the
small JSON contract helpers below.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PINNED_VERSION = "0.19.1"
REPOSITORY_FORMAT = 2
EDIT_FRACTION_LIMIT = 0.50
DEFAULT_EVIDENCE_FILE = Path(tempfile.gettempdir()) / "josh-room-restic-phase0-evidence.json"


class ProbeError(RuntimeError):
    """Raised when a probe gate cannot be proven."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeError(message)


@dataclass(frozen=True)
class S3ProbeConfig:
    provider: str
    repository: str = field(repr=False)
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)
    region: str = "us-east-1"

    def environment(self, run_id: str) -> dict[str, str]:
        env = {
            "RESTIC_REPOSITORY": f"{self.repository}/probe-{run_id}",
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_DEFAULT_REGION": self.region,
        }
        if self.session_token:
            env["AWS_SESSION_TOKEN"] = self.session_token
        return env


def parse_s3_config(payload: str) -> S3ProbeConfig:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProbeError("invalid S3 probe configuration JSON") from exc
    if not isinstance(data, dict):
        raise ProbeError("S3 probe configuration must be a JSON object")
    provider = data.get("provider")
    if provider not in {"minio", "r2"}:
        raise ProbeError("S3 probe provider must be minio or r2")
    repository = data.get("repository")
    if not isinstance(repository, str) or not repository.startswith("s3:https://"):
        raise ProbeError("S3 probe repository must use an HTTPS S3 URL")
    try:
        parts = urlsplit(repository[3:])
        hostname = parts.hostname
    except ValueError as exc:
        raise ProbeError("invalid S3 probe repository URL") from exc
    path_parts = [part for part in parts.path.split("/") if part]
    if (
        not hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or len(path_parts) != 2
        or path_parts[-1] != "room-store-phase0"
    ):
        raise ProbeError(
            "S3 probe repository must target an existing bucket's fixed room-store-phase0 prefix"
        )
    access_key_id = data.get("access_key_id")
    secret_access_key = data.get("secret_access_key")
    session_token = data.get("session_token")
    region = data.get("region", "us-east-1")
    if not isinstance(access_key_id, str) or not access_key_id:
        raise ProbeError("S3 probe access key is missing")
    if not isinstance(secret_access_key, str) or not secret_access_key:
        raise ProbeError("S3 probe secret key is missing")
    if session_token is not None and not isinstance(session_token, str):
        raise ProbeError("S3 probe session token is invalid")
    if not isinstance(region, str) or not region or any(c.isspace() for c in region):
        raise ProbeError("S3 probe region is invalid")
    return S3ProbeConfig(
        provider=provider,
        repository=repository,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=session_token,
        region=region,
    )


def parse_json_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"invalid restic JSON at line {line_number}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("message_type"), str):
            raise ProbeError(f"invalid restic event shape at line {line_number}")
        events.append(event)
    return events


def summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [event for event in events if event["message_type"] == "summary"]
    if len(summaries) != 1 or not events or events[-1] is not summaries[0]:
        raise ProbeError("expected exactly one terminal restic summary")
    summary = summaries[0]
    error_events = sum(event["message_type"] == "error" for event in events)
    summary_errors = summary.get("errors", error_events)
    if not isinstance(summary_errors, int) or summary_errors != error_events:
        raise ProbeError("restic summary error count disagrees with error events")
    if summary_errors != 0:
        raise ProbeError("restic backup reported errors")
    fields = (
        "snapshot_id",
        "files_new",
        "files_changed",
        "files_unmodified",
        "data_added",
        "data_added_packed",
        "total_bytes_processed",
        "errors",
    )
    result: dict[str, Any] = {"summary_count": len(summaries)}
    for field_name in fields:
        value = summary.get(field_name)
        if field_name == "snapshot_id":
            if value is not None and not isinstance(value, str):
                raise ProbeError("invalid snapshot ID in restic summary")
        elif field_name == "errors":
            continue
        elif value is not None and (not isinstance(value, int) or value < 0):
            raise ProbeError(f"invalid {field_name} in restic summary")
        result[field_name] = value
    result["errors"] = summary_errors
    return result


def _version(binary: str) -> str:
    result = subprocess.run(
        [binary, "version"], check=False, capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        raise ProbeError("restic version command failed")
    version = result.stdout.strip().split()
    if len(version) < 2 or version[0] != "restic":
        raise ProbeError("unrecognized restic version output")
    return version[1]


def _restic(
    binary: str,
    env: dict[str, str],
    *args: str,
    json_output: bool = False,
    cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]]]:
    command = [binary, *args]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=300,
    )
    if result.returncode != 0:
        # Do not include restic diagnostics: they can contain filesystem paths.
        raise ProbeError(f"restic {args[0]} failed with exit status {result.returncode}")
    return result, parse_json_events(result.stdout) if json_output else []


def _backup(
    binary: str, env: dict[str, str], source: Path, parent: str | None = None
) -> dict[str, Any]:
    args = ["backup", "--json", "--skip-if-unchanged"]
    if parent:
        args.extend(["--parent", parent])
    args.extend(["--", "."])
    _, events = _restic(binary, env, *args, json_output=True, cwd=source)
    return summarize_events(events)


def _snapshots(binary: str, env: dict[str, str]) -> list[dict[str, Any]]:
    result, _ = _restic(binary, env, "snapshots", "--json")
    payload = json.loads(result.stdout)
    if not isinstance(payload, list):
        raise ProbeError("restic snapshots returned an invalid result")
    return payload


def _terminate_owned(process, platform: str) -> None:
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    from josh_room.cancellation import terminate_owned_process

    terminate_owned_process(process, platform=platform)


def _interrupt_backup(
    binary: str,
    env: dict[str, str],
    source: Path,
    *,
    platform: str | None = None,
    terminate_process=_terminate_owned,
) -> dict[str, Any]:
    platform = platform or os.name
    popen_options: dict[str, Any] = {}
    if platform == "nt":
        popen_options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(
        [binary, "backup", "--json", "--verbose", "--", "."],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        env=env,
        cwd=source,
        bufsize=1,
        **popen_options,
    )
    assert process.stdout is not None
    cancelled = False
    observed_message_type = None
    try:
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                observed_message_type = "invalid_json"
                continue
            if isinstance(event, dict):
                observed_message_type = event.get("message_type")
            if isinstance(event, dict) and event.get("message_type") in {
                "status",
                "verbose_status",
            }:
                if process.poll() is None:
                    _cancel_process(process, platform, terminate_process)
                    cancelled = True
                break
        if not cancelled and process.poll() is None:
            _cancel_process(process, platform, terminate_process)
            cancelled = True
        return {
            "signal_sent": cancelled,
            "exit_code": process.wait(timeout=30),
            "observed_message_type": observed_message_type,
        }
    finally:
        if process.poll() is None:
            terminate_process(process, platform)
        process.stdout.close()


def _cancel_process(process, platform: str, terminate_process) -> None:
    if platform != "nt":
        process.send_signal(signal.SIGINT)
        return
    ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
    if ctrl_break is None:
        terminate_process(process, platform)
        return
    try:
        process.send_signal(ctrl_break)
    except (OSError, ValueError):
        terminate_process(process, platform)


def _write_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "README.txt").write_text("synthetic Room Store fixture\n")
    (root / "src" / "payload.bin").write_bytes(secrets.token_bytes(8 * 1024 * 1024))
    (root / "data").mkdir()
    (root / "data" / "record.json").write_text('{"fixture":true}\n')


def _write_cancel_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    for index in range(2048):
        (root / f"cancel-{index:04d}.txt").write_bytes(b"synthetic-cancel-fixture\n")


def _persist_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_probe(
    binary: str,
    evidence_file: Path = DEFAULT_EVIDENCE_FILE,
    remote: S3ProbeConfig | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "probe_version": 1,
        "engine": "restic",
        "backend": "s3-compatible" if remote else "local-filesystem",
        "status": "running",
        "stages": [],
        "unproved": [],
    }
    if remote:
        evidence["provider"] = remote.provider
    evidence["unproved"] = [
        "minio",
        "r2",
        "Linux local runtime",
        "Windows local runtime",
        "packaged controller/runtime installation",
    ]
    _persist_evidence(evidence_file, evidence)

    def stage(
        name: str,
        operation: Any,
        accept: Any = None,
    ) -> Any:
        item = {"name": name, "status": "running"}
        evidence["stages"].append(item)
        _persist_evidence(evidence_file, evidence)
        try:
            result = operation()
            item["evidence"] = result
            _persist_evidence(evidence_file, evidence)
            if accept is not None:
                accept(result)
        except Exception as exc:
            item["status"] = "failed"
            item["error"] = (
                str(exc)
                if isinstance(exc, ProbeError)
                else f"probe stage failed ({type(exc).__name__})"
            )
            evidence["status"] = "failed"
            evidence["failed_stage"] = name
            _persist_evidence(evidence_file, evidence)
            if isinstance(exc, ProbeError):
                raise
            raise ProbeError(item["error"]) from exc
        item["status"] = "passed"
        _persist_evidence(evidence_file, evidence)
        return result

    evidence["host_platform"] = sys.platform

    version = stage("version", lambda: _version(binary))
    stage(
        "version_pin",
        lambda: version,
        accept=lambda actual: _require(
            actual == PINNED_VERSION,
            f"expected restic {PINNED_VERSION}; found {actual}",
        ),
    )
    evidence["engine_version"] = version

    with tempfile.TemporaryDirectory(prefix="josh-room-restic-probe-") as temporary:
        root = Path(temporary)
        repository = root / "repository"
        cache = root / "cache"
        workspace = root / "workspace"
        password_file = root / "synthetic-restic-password"

        def prepare_local_inputs() -> bool:
            password_file.write_text(secrets.token_urlsafe(32) + "\n")
            password_file.chmod(0o600)
            _write_fixture(workspace)
            cache.mkdir()
            return True

        stage("prepare_synthetic_inputs", prepare_local_inputs)
        env = os.environ.copy()
        for key in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
            "AWS_SHARED_CREDENTIALS_FILE",
            "AWS_CONFIG_FILE",
        ):
            env.pop(key, None)
        env.update(
            {
                "RESTIC_REPOSITORY": str(repository),
                "RESTIC_PASSWORD_FILE": str(password_file),
                "RESTIC_CACHE_DIR": str(cache),
                "RESTIC_PROGRESS_FPS": "1",
                "LC_ALL": "C",
            }
        )
        if remote:
            env.update(remote.environment(secrets.token_hex(8)))

        stage(
            "repository_init",
            lambda: _restic(
                binary, env, "init", "--repository-version", str(REPOSITORY_FORMAT)
            )[0].returncode,
            accept=lambda status: _require(status == 0, "restic init failed"),
        )

        def read_repository_format() -> int:
            result, _ = _restic(binary, env, "cat", "config")
            config = json.loads(result.stdout)
            return config.get("version")

        stage(
            "repository_format",
            read_repository_format,
            accept=lambda actual: _require(
                actual == REPOSITORY_FORMAT,
                "restic repository format did not match explicit format 2",
            ),
        )

        def initial_backup() -> dict[str, Any]:
            return _backup(binary, env, workspace)

        initial = stage(
            "initial_backup",
            initial_backup,
            accept=lambda result: _require(
                bool(result.get("snapshot_id")), "initial backup omitted snapshot ID"
            ),
        )
        initial_id = str(initial["snapshot_id"])

        restored = root / "restored"
        def restore_and_compare() -> bool:
            _restic(binary, env, "restore", initial_id, "--target", str(restored))
            for relative in ("src/README.txt", "src/payload.bin", "data/record.json"):
                if (restored / relative).read_bytes() != (workspace / relative).read_bytes():
                    raise ProbeError("restored fixture content did not match the source")
            return True

        stage("initial_restore", restore_and_compare)

        # This is a separate restic process, intentionally proving parent
        # selection does not depend on hostname/path auto-selection.
        def run_noop() -> dict[str, Any]:
            before = len(_snapshots(binary, env))
            summary = _backup(binary, env, workspace, parent=initial_id)
            after = len(_snapshots(binary, env))
            return {"before_snapshot_count": before, "summary": summary, "after_snapshot_count": after}

        stage(
            "explicit_parent_noop",
            run_noop,
            accept=lambda result: _require(
                result["summary"].get("snapshot_id") is None
                and result["before_snapshot_count"] == result["after_snapshot_count"],
                "unchanged backup created a new snapshot",
            ),
        )

        payload = workspace / "src" / "payload.bin"

        def apply_small_edit() -> bool:
            with payload.open("r+b") as stream:
                stream.seek(1024 * 1024)
                stream.write(b"changed-bytes")
            return True

        stage("apply_small_edit", apply_small_edit)
        edited = stage(
            "small_edit",
            lambda: _backup(binary, env, workspace, parent=initial_id),
            accept=lambda result: _require(
                bool(result.get("snapshot_id"))
                and (result.get("data_added_packed") or 0)
                <= (result.get("total_bytes_processed") or 1) * EDIT_FRACTION_LIMIT,
                "small edit exceeded the bounded packed-data fraction",
            ),
        )
        processed = edited.get("total_bytes_processed") or 1
        added = edited.get("data_added_packed") or 0
        edited_id = str(edited["snapshot_id"])

        renamed = workspace / "src" / "renamed-payload.bin"

        def apply_rename() -> bool:
            payload.rename(renamed)
            return True

        stage("apply_rename", apply_rename)
        rename_result = stage(
            "rename_reuse",
            lambda: _backup(binary, env, workspace, parent=edited_id),
            accept=lambda result: _require(
                bool(result.get("snapshot_id"))
                and (result.get("data_added_packed") or 0) <= max(1024 * 1024, added * 2)
                ,
                "rename uploaded an unexpectedly large packed-data amount",
            ),
        )

        # Verbose JSON is enabled only here. A small bounded namespace fixture
        # produces an active progress record without uploading a large payload.
        cancel_source = root / "cancel-workspace"

        def prepare_cancel_fixture() -> bool:
            _write_cancel_fixture(cancel_source)
            return True

        stage("prepare_cancellation_fixture", prepare_cancel_fixture)
        cancellation = stage(
            "cancelled_backup",
            lambda: _interrupt_backup(binary, env, cancel_source),
            accept=lambda result: _require(
                result["signal_sent"] and result["exit_code"] != 0,
                "restic backup completed before the cancellation signal",
            ),
        )
        stage("check_after_cancel", lambda: _restic(binary, env, "check")[0].returncode)

        # Concurrent writers use independent synthetic roots and a known parent.
        concurrent_roots = [root / "concurrent-a", root / "concurrent-b"]

        def prepare_concurrent_inputs() -> bool:
            for index, path in enumerate(concurrent_roots):
                shutil.copytree(workspace, path)
                (path / f"concurrent-{index}.txt").write_text(f"writer-{index}\n")
            return True

        stage("prepare_concurrent_inputs", prepare_concurrent_inputs)
        def run_concurrent() -> list[dict[str, Any]]:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _backup, binary, env, path, str(rename_result["snapshot_id"])
                    )
                    for path in concurrent_roots
                ]
                return [future.result() for future in futures]

        concurrent_results = stage(
            "concurrent_backups",
            run_concurrent,
            accept=lambda results: _require(
                len(results) == 2 and all(result.get("snapshot_id") for result in results),
                "concurrent backup omitted a snapshot ID",
            ),
        )
        stage("final_repository_check", lambda: _restic(binary, env, "check")[0].returncode)

        evidence.update(
            {
                "engine_version": version,
                "repository_format": REPOSITORY_FORMAT,
                "status": "passed",
                "steps": {
                    "initialized": True,
                    "initial_backup_restore": True,
                    "explicit_parent_fresh_process": True,
                    "skip_if_unchanged": True,
                    "small_edit_bounded": True,
                    "rename_reused_data": True,
                    "cancelled_backup_repository_usable": True,
                    "concurrent_backups_integrity": True,
                    "json_summary_contract": True,
                },
                "metrics": {
                    "initial_data_added_packed": initial.get("data_added_packed"),
                    "edit_data_added_packed": edited.get("data_added_packed"),
                    "edit_data_processed": processed,
                    "edit_fraction_limit": EDIT_FRACTION_LIMIT,
                    "rename_data_added_packed": rename_result.get("data_added_packed"),
                    "concurrent_snapshot_count": len(concurrent_results),
                    "cancelled_exit_code": cancellation["exit_code"],
                    "cancel_progress_message_type": cancellation["observed_message_type"],
                },
            }
        )
        if remote:
            evidence["unproved"].remove(remote.provider)
        current_platform_gate = (
            "Windows local runtime" if sys.platform.startswith("win") else
            "Linux local runtime" if sys.platform.startswith("linux") else None
        )
        if current_platform_gate:
            evidence["unproved"].remove(current_platform_gate)
        _persist_evidence(evidence_file, evidence)
        return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restic",
        default=os.environ.get("JOSH_ROOM_RESTIC")
        or shutil.which("restic")
        or "/home/linuxbrew/.linuxbrew/bin/restic",
        help="path to the pinned restic 0.19.1 executable",
    )
    parser.add_argument(
        "--evidence-file",
        type=Path,
        default=DEFAULT_EVIDENCE_FILE,
        help="private path for atomically updated stage evidence",
    )
    parser.add_argument(
        "--s3-config-stdin",
        action="store_true",
        help="read remote S3 probe settings and credentials as JSON from stdin",
    )
    args = parser.parse_args(argv)
    try:
        remote = parse_s3_config(sys.stdin.read()) if args.s3_config_stdin else None
        result = run_probe(args.restic, evidence_file=args.evidence_file, remote=remote)
    except OSError:
        print(
            json.dumps(
                {"probe_version": 1, "passed": False, "error": "probe could not start restic"}
            ),
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print(
            json.dumps(
                {"probe_version": 1, "passed": False, "error": "restic command timed out"}
            ),
            file=sys.stderr,
        )
        return 1
    except ProbeError as exc:
        print(json.dumps({"probe_version": 1, "passed": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
