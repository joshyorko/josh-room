"""RCC-native Browser tasks: interactive batches, deterministic replay and controls."""
import json
import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from robocorp import browser
from robocorp.tasks import get_output_dir, task

from actions import Workbench
from contracts import Ledger
from target import Target
from doctor import diagnose


def emit(ledger, result):
    payload = json.dumps(result, separators=(",", ":"))
    ledger.charge("output_bytes", len(payload.encode()) + 13)
    print("PILOT_STATE " + payload, file=sys.__stdout__, flush=True)


def execute(requests=None):
    output = Path(get_output_dir()) / "mission"
    output.mkdir()
    mission = json.loads((Path(__file__).parent / "mission.json").read_text())
    ledger = Ledger(mission, output)
    def expired(*_):
        raise TimeoutError("Mission time budget or termination")
    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)
    signal.alarm(mission["budgets"]["seconds"])
    errors = []
    workbench = None
    try:
        with Target(os.environ["PILOT_VSIX"], output) as target:
            doctor_result = diagnose(output, Path(os.environ["PILOT_VSIX"]))
            workbench = Workbench(target, ledger)
            browser_version = browser.browser().version
            emit(ledger, {"ready": True, "schema_version": 1, "browser": browser_version,
                          "receipt": "mission/target.json", "transport": "RCC bounded file inbox",
                          "binding": workbench.fence.binding(), "doctor": doctor_result})
            iterator = iter(requests) if requests is not None else None
            inbox = Path(os.environ["PILOT_INBOX"]) if os.environ.get("PILOT_INBOX") else None
            inbox_sequence = 1
            while True:
                if iterator is None:
                    if inbox is not None:
                        message = inbox / f"{inbox_sequence:04}.json"
                        while not message.exists():
                            time.sleep(0.2)
                        if message.stat().st_size > 16000:
                            raise ValueError("Batch message size ceiling")
                        line = message.read_text()
                        inbox_sequence += 1
                    else:
                        line = sys.__stdin__.readline(16001)
                    if not line:
                        break
                    request = json.loads(line)
                else:
                    try:
                        request = next(iterator)
                    except StopIteration:
                        break
                if request.get("stop") is True:
                    break
                if iterator is not None:
                    request = {**request, "binding": workbench.fence.binding()}
                result = workbench.batch(request)
                emit(ledger, result)
                if not result["ok"]:
                    errors.append(result["error"])
                    break
    except BaseException as error:
        errors.append({"type": type(error).__name__, "message": str(error)[:1000]})
        raise
    finally:
        signal.alarm(0)
        if workbench is not None:
            workbench.fence = workbench.fence.close()
            browser.browser().close()
        summary = ledger.persist(errors=errors)
        total = sum(p.stat().st_size for p in Path(get_output_dir()).rglob("*") if p.is_file())
        if total > mission["budgets"]["evidence_bytes"]:
            raise RuntimeError("Total artifact budget exceeded")
        emit(ledger, {"complete": True, "ok": not errors, "counts": summary["counts"],
                      "evidence_bytes_observed_before_task_log_finalization": total,
                      "receipt": "mission/ledger.json"})
        ledger.persist(errors=errors)
    if errors:
        raise AssertionError(errors[0])


@task
def explore_batches():
    """Accept bounded reusable actions until explicit stop, EOF or mission budget."""
    execute()


@task
def replay_batches():
    """Replay admitted actions in a fresh fixture/profile/installed host."""
    recorded = json.loads(Path(os.environ["PILOT_REPLAY"]).read_text())
    raw = Path(os.environ["PILOT_REPLAY"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != os.environ["PILOT_REPLAY_SHA256"]:
        raise ValueError("Reproduction evidence digest changed")
    attempts = recorded["batches"][-1]["result"]["binding"]["attempt_set"]
    if attempts != json.loads(os.environ["PILOT_REPLAY_ATTEMPTS"]):
        raise ValueError("Reproduction attempt-set changed")
    candidate = recorded["batches"][-1]["result"]["binding"]["candidate_sha256"]
    if hashlib.sha256(Path(os.environ["PILOT_VSIX"]).read_bytes()).hexdigest() != candidate:
        raise ValueError("Reproduction candidate changed")
    for row in recorded["batches"]:
        for evidence in row["result"]["receipts"]:
            artifact = Path(os.environ["PILOT_REPLAY"]).parent / evidence["path"]
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != evidence["sha256"]:
                raise ValueError("Reproduction observation retention failed")
    (Path(get_output_dir()) / "reproduction.json").write_text(json.dumps({"evidence_sha256": os.environ["PILOT_REPLAY_SHA256"],
        "source_attempt_set": attempts, "candidate_sha256": candidate, "fresh_target_rebound_explicitly": True}, indent=2))
    execute([record["request"] for record in recorded["batches"]])


@task
def verify_selection():
    """Deterministic verification lane, separate from adaptive exploration."""
    execute([{"schema_version": 1, "actions": [
        {"kind": "command", "command": "joshRoom.saveCatalog"},
        {"kind": "folder", "path": "/pilot/workspace/projects"},
        {"kind": "assert_text", "text": "Catalog children"},
        {"kind": "choose", "label": "Browse all folders"},
        {"kind": "choose", "label": "Select All on This Page (2)"},
        {"kind": "choose", "label": "Continue with 2 selected"},
        {"kind": "assert_text", "text": "Save 2 selected folders as Rooms"}
    ]}])


@task
def contract_checks():
    """RCC checks of budget controls and the actual product pin guard."""
    root = Path(__file__).parent
    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(root), "-p", "test_contracts.py"], check=True)
    subprocess.run([sys.executable, str(root / "verification/check_product_pins.py"),
                    str(Path(get_output_dir()) / "product-pin-check.json")], check=True)


@task
def catalog_select_all_regression():
    """Known defect control: bulk selection must not reject its own overlap."""
    execute([{"schema_version": 1, "actions": [
        {"kind": "command", "command": "joshRoom.saveCatalog"},
        {"kind": "folder", "path": "/pilot/workspace"},
        {"kind": "assert_text", "text": "Catalog children"},
        {"kind": "choose", "label": "Browse all folders"},
        {"kind": "choose", "label": "Select All on This Page"},
        {"kind": "assert_selection"}
    ]}])


@task
def clipboard_adapter_check():
    """Harness-only real-browser delayed clipboard control; no product claim."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from actions import read_receipt
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"""<button onclick='setTimeout(()=>navigator.clipboard.writeText(JSON.stringify({namespace: "synthetic",parent_snapshot_id: "point"})),200)'>Copy recovery receipt</button>""")
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        browser.configure(browser_engine="chrome", headless=True, install=False, screenshot="off")
        browser.configure_context(permissions=["clipboard-read", "clipboard-write"])
        page = browser.goto(f"http://127.0.0.1:{server.server_port}")
        page.evaluate("navigator.clipboard.writeText('')")
        page.get_by_text("Copy recovery receipt", exact=True).click()
        calls = []
        def charged(fn, *args):
            calls.append(1)
            return fn(*args)
        assert read_receipt(page, charged) == {"namespace": "synthetic", "parent_snapshot_id": "point"}
        (Path(get_output_dir()) / "clipboard-control.json").write_text(json.dumps({"pass": True, "delay_ms": 200, "reads": len(calls), "scope": "browser adapter only"}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        browser.browser().close()


if __name__ == "__main__":
    output = Path(os.environ["PILOT_OUTPUT"]).resolve()
    status = subprocess.call([sys.executable, "-m", "robocorp.tasks", "run", str(Path(__file__).resolve()),
        "-t", sys.argv[1], "--output-dir", str(output)])
    size = sum(p.stat().st_size for p in output.rglob("*") if p.is_file())
    limit = json.loads(Path(__file__).with_name("mission.json").read_text())["budgets"]["evidence_bytes"]
    (output / "finalized-artifacts.json").write_text(json.dumps({"bytes": size, "limit": limit, "within_budget": size <= limit, "task_exit": status}))
    raise SystemExit(status if size <= limit else 3)
