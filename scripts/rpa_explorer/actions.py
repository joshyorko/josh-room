"""Reusable visible-workbench actions; no product API patches or arbitrary eval."""
import json
import hashlib
import re
import time
from pathlib import PurePosixPath

from robocorp import browser
from ownership import SessionFence


def read_receipt(page, call):
    deadline = time.monotonic() + 5
    while True:
        raw = call(page.evaluate, "navigator.clipboard.readText()")
        try:
            value = json.loads(raw)
            if isinstance(value, dict) and value.get("parent_snapshot_id") and value.get("namespace"):
                return value
        except ValueError:
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError("Public recovery receipt clipboard write did not complete")
        page.wait_for_timeout(100)


class Workbench:
    def __init__(self, target, ledger):
        self.target, self.ledger = target, ledger
        browser.configure(browser_engine="chrome", headless=True, install=False, screenshot="off")
        browser.configure_context(viewport={"width": 1280, "height": 800})
        self.page = browser.goto(target.url)
        self.page.set_default_timeout(20000)
        self.page.locator(".monaco-workbench").wait_for(timeout=60000)
        self.titles = {item["command"]: item["title"] for item in target.package["contributes"]["commands"]}
        self.dispatched_commands = set()
        self.fence = SessionFence(target.name, 1, target.provenance["vsix_sha256"], "0" * 64, (target.name,))

    def call(self, function, *args, **kwargs):
        self.ledger.charge("ui_calls")
        return function(*args, **kwargs)

    def transition(self, previous):
        self.page.wait_for_function("""previous => {
            const input = document.querySelector('.quick-input-widget input');
            return !input || !input.getBoundingClientRect().height || input.value !== previous;
        }""", arg=previous)

    def command(self, command):
        if command not in self.ledger.mission["commands"]:
            raise ValueError("Command outside mission")
        title = self.titles[command]
        self.call(self.page.keyboard.press, "F1")
        value = ">" + title
        self.call(self.page.locator(".quick-input-widget input").first.fill, value)
        self.call(self.page.locator(".quick-input-list .monaco-list-row").filter(has_text=title).first.click)
        self.dispatched_commands.add(command)
        self.transition(value)

    def choose(self, label):
        self.call(self.page.locator(".quick-input-widget input").first.fill, label)
        exact = self.page.locator(".quick-input-list").get_by_text(label, exact=True)
        selected = exact.first if exact.count() else self.page.locator(".quick-input-list .monaco-list-row").filter(has_text=label).first
        self.call(selected.click)
        self.transition(label)

    def folder(self, path):
        parsed = PurePosixPath(path)
        if ".." in parsed.parts or not (path == "/pilot/workspace" or path.startswith("/pilot/workspace/") or path.startswith("/pilot/cold/")):
            raise ValueError("Folder outside synthetic workspace")
        widget = self.page.locator(".quick-input-widget")
        box = widget.get_by_placeholder("Folder path")
        box.wait_for()
        if box.input_value().rstrip("/") != path:
            self.call(box.fill, path + "/")
            self.page.wait_for_function("""path => {
                const widget = document.querySelector('.quick-input-widget');
                const input = widget?.querySelector('input');
                return input?.value.replace(/\\/$/, '') === path &&
                    [...widget.querySelectorAll('.monaco-list-row')].some(row => row.innerText.trim() === '..');
            }""", arg=path)
        self.call(widget.get_by_role("button", name="OK", exact=True).click)
        box.wait_for(state="hidden")

    def observe(self, batch):
        self.call(self.page.locator("body").inner_text)  # Waits for an attached workbench.
        state = self.page.evaluate("""() => {
            const widget = document.querySelector('.quick-input-widget');
            const visible = widget && widget.getBoundingClientRect().height > 0;
            return {title: visible ? widget.querySelector('.quick-input-title')?.innerText || '' : '',
                placeholder: visible ? widget.querySelector('input')?.placeholder || '' : '',
                rows: visible ? [...widget.querySelectorAll('.monaco-list-row')].slice(0,10).map(x=>x.innerText) : [],
                checked: visible ? [...widget.querySelectorAll('[role=checkbox]')].map(x=>({label:x.getAttribute('aria-label'),checked:x.getAttribute('aria-checked')})) : [],
                notices: [...document.querySelectorAll('.notification-list-item')].map(x=>x.innerText.slice(0,500)),
                dialog: document.querySelector('.monaco-dialog-box')?.innerText?.slice(0,1000) || ''};
        }""")
        text = self.page.locator("body").inner_text()[:18000]
        refs = [self.ledger.write(f"batch-{batch:02}.txt", text.encode()),
                self.ledger.write(f"batch-{batch:02}.png", self.page.screenshot())]
        return {"schema_version": 1, "batch": batch, "state": state, "receipts": refs}

    def action(self, action):
        self.ledger.admit(action)
        kind = action["kind"]
        if kind == "command":
            self.command(action["command"])
        elif kind == "choose":
            self.choose(action["label"])
        elif kind == "folder":
            self.folder(action["path"])
        elif kind == "text":
            self.call(self.page.locator(".quick-input-widget input").first.fill, action["value"])
            self.call(self.page.keyboard.press, "Enter")
        elif kind == "dismiss":
            self.call(self.page.keyboard.press, "Escape")
        elif kind == "button":
            self.call(self.page.get_by_role("button", name=action["label"], exact=True).click)
        elif kind == "assert_text":
            timeout = action.get("timeout_ms", 60000)
            if not 1 <= timeout <= 60000:
                raise ValueError("Assertion wait outside budget")
            self.page.get_by_text(action["text"], exact=False).first.wait_for(timeout=timeout)
        elif kind == "assert_selection":
            self.page.wait_for_function("""() => /Catalog children — [1-9][0-9]* selected|Select either/.test(document.body.innerText)""", timeout=5000)
            assert "Select either" not in self.page.locator("body").inner_text(), "KNOWN_CONTROL: Catalog bulk selection rejected its own overlap"
        elif kind == "fixture":
            self.fixture_receipts.append(self.target.fixture_operation(action["operation"]))
        elif kind == "copy_recovery_receipt":
            # Private browser context; this is the product's explicit public receipt action.
            self.page.context.grant_permissions(["clipboard-read", "clipboard-write"])
            self.call(self.page.evaluate, "navigator.clipboard.writeText('')")
            if self.call(self.page.evaluate, "navigator.clipboard.readText()") != "":
                raise AssertionError("Disposable clipboard did not clear")
            saved_notice = self.page.locator(".notification-list-item").filter(
                has_text="Catalog saved with 1 child Room."
            )
            receipt_button = saved_notice.get_by_text("Copy recovery receipt", exact=True)
            if not receipt_button.is_visible():
                self.call(self.page.locator('[id="status.notifications"]').click)
            self.call(receipt_button.filter(visible=True).click)
            point = read_receipt(self.page, self.call)
            if set(point) != {"dimension_id", "namespace", "parent_room_id", "parent_snapshot_id"} or any(not isinstance(x, str) or not re.fullmatch(r"[a-z0-9-]{1,100}", x) for x in point.values()):
                raise ValueError("Unexpected recovery receipt contract")
            payload = json.dumps(point, indent=2).encode()
            self.ledger.write("ui-recovery-receipt.json", payload)
            (self.target.root / "ui-recovery-receipt.json").write_bytes(payload)
            self.fixture_receipts.append({"ui_recovery_receipt": point})
        elif kind == "open_cold_parent":
            if not (self.target.root / "cold-receipt.json").exists():
                raise ValueError("Cold parent restore receipt required before navigation")
            self.call(self.page.goto, self.target.url.replace("/pilot/workspace", "/pilot/cold/parent"))
            self.page.locator(".monaco-workbench").wait_for(timeout=60000)
        elif kind == "decision":
            if not self.ledger.records:
                raise ValueError("Adaptive decision requires a prior UI observation")
            self.ledger.decisions.append({"after_batch": len(self.ledger.records), "reason": action["reason"], "classification": action["classification"]})
        else:
            raise ValueError("Unsupported action")

    def batch(self, request):
        if request.get("schema_version") != 1 or not isinstance(request.get("actions"), list) or len(request["actions"]) > 12:
            raise ValueError("Invalid batch contract")
        self.ledger.charge("batches")
        number = self.ledger.counts["batches"]
        self.fixture_receipts = []
        error = None
        try:
            self.fence.admit(request)
            for action in request["actions"]:
                self.fence.admit(request)
                self.action(action)
        except Exception as failure:
            error = {"type": type(failure).__name__, "message": str(failure)[:1000]}
        result = self.observe(number)
        if self.fixture_receipts:
            result["fixture_receipts"] = self.fixture_receipts
        observed = hashlib.sha256(json.dumps({"state": result["state"], "receipts": result["receipts"]}, sort_keys=True).encode()).hexdigest()
        self.fence = self.fence.observe(observed)
        result["binding"] = self.fence.binding()
        result.update(ok=error is None, error=error, counts=dict(self.ledger.counts))
        self.ledger.records.append({"request": request, "result": result})
        surface_path = self.ledger.output / "surface.json"
        surface = json.loads(surface_path.read_text())
        for command in self.dispatched_commands:
            surface["coverage"][command] = "DISPATCHED; see the batch receipt for the observed outcome"
        surface_path.write_text(json.dumps(surface, indent=2) + "\n")
        payload = json.dumps(result, separators=(",", ":"))
        self.ledger.persist()
        return result
