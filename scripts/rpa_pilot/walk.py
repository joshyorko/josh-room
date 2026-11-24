"""Bounded installed-extension UI walk; Playwright fallback, not Codex code mode.

Only point this driver at a task-owned editor with synthetic data. Setup and
provider qualification are deliberately separate from visible interaction.
"""
import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def walk(url, output, chrome, phase):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    ledger = {"driver": "Playwright fallback (not code mode)", "phase": phase,
              "budget": {"seconds": 600, "actions": 60}, "events": []}
    actions = 0

    def record(name, **data):
        ledger["events"].append({"phase": name, "seconds": round(time.monotonic() - started, 2), **data})
        (output / "walk.json").write_text(json.dumps(ledger, indent=2) + "\n")

    def act(fn, *args, **kwargs):
        nonlocal actions
        actions += 1
        if actions > 60 or time.monotonic() - started > 600:
            raise RuntimeError("Pilot budget exhausted")
        return fn(*args, **kwargs)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome, headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.set_default_timeout(15000)

        def evidence(name):
            page.screenshot(path=str(output / (name + ".png")))
            text = page.locator("body").inner_text()
            (output / (name + ".txt")).write_text(text[:24000])
            record(name, text=text[:12000], actions=actions)

        def command(title):
            act(page.keyboard.press, "F1")
            box = page.locator(".quick-input-widget input").first
            box.wait_for(state="visible")
            act(box.fill, ">" + title)
            row = page.locator(".quick-input-list .monaco-list-row").filter(has_text=title).first
            row.wait_for(state="visible")
            act(row.click)
            page.wait_for_function("""value => {
                const input = document.querySelector('.quick-input-widget input');
                return !input || !input.getBoundingClientRect().height || input.value !== value;
            }""", arg=">" + title)
            record("command", title=title)

        def choose(label):
            # Monaco virtualizes off-screen rows; filter through its visible input.
            box = page.locator(".quick-input-widget input").first
            box.wait_for(state="visible")
            act(box.fill, label)
            row = page.locator(".quick-input-list .monaco-list-row").filter(has_text=label).first
            row.wait_for(state="visible")
            act(row.click)
            page.wait_for_function("""label => {
                const input = document.querySelector('.quick-input-widget input');
                return !input || !input.getBoundingClientRect().height || input.value !== label;
            }""", arg=label)

        try:
            act(page.goto, url, wait_until="domcontentloaded", timeout=60000)
            page.locator(".monaco-workbench").wait_for(timeout=60000)
            record("browser", version=browser.version)
            evidence("workbench")
            if phase == "activation":
                command("Josh: Check Workspace")
                # Observe a semantic controller response, not just command dispatch.
                page.get_by_text("Workspace valid ·", exact=False).first.wait_for(timeout=240000)
                evidence("workspace-check")
                command("Developer: Show Running Extensions")
                page.get_by_text("Josh Room", exact=False).first.wait_for()
                evidence("running-extensions")
            elif phase == "inspect":
                evidence("inspect")
            elif phase == "catalog":
                command("Josh: Enter Child Room")
                page.get_by_text("Open a restored Catalog parent first.", exact=False).first.wait_for()
                evidence("hydration-precondition")
                command("Josh: Save Catalog Room")
                page.locator(".quick-input-widget").wait_for(state="visible")
                evidence("catalog-source-dialog")
                act(page.locator(".quick-input-widget .monaco-button").first.click)
                page.get_by_text("Catalog children —", exact=False).first.wait_for(timeout=60000)
                evidence("catalog-suggestions")
                choose("Browse all folders")
                evidence("catalog-all-folders")
                choose("Select All on This Page")
                page.wait_for_function("""() => /Select either deep\\/level-00 or its enclosing folder|Catalog children — [1-9][0-9]* selected/.test(document.body.innerText)""")
                evidence("catalog-select-all")
                if "Select either deep/level-00 or its enclosing folder" in page.locator("body").inner_text():
                    ledger["product_failure"] = {
                        "id": "catalog-select-all-overlap", "command": "joshRoom.saveCatalog",
                        "expected": "Select All selects a non-overlapping set of children",
                        "observed": "Bulk action rejected its own enclosing-folder/descendant selection",
                        "regression": "catalog_select_all in robot.yaml",
                    }
                    raise AssertionError("PRODUCT_FAILURE: catalog-select-all-overlap")
                act(page.keyboard.press, "Escape")
            else:
                command("Josh: New Room")
                page.get_by_text("Configure Catalog Room", exact=True).wait_for()
                evidence("new-room")
                act(page.keyboard.press, "Escape")
                command("Josh: Connect Storage")
                page.locator(".quick-input-widget").wait_for(state="visible")
                evidence("storage-choices")
                act(page.keyboard.press, "Escape")
                command("Josh: Split Workspace into Rooms (Preview Only)")
                page.locator(".quick-input-widget").wait_for(state="visible")
                evidence("split-folder-dialog")
                act(page.get_by_role("button", name="Preview this folder", exact=True).click)
                page.get_by_text("Split preview: partial coverage; 82 indexed", exact=True).wait_for(timeout=60000)
                evidence("split-index")
                choose("Suggested repository boundaries")
                evidence("split-suggestions")
                choose("Browse all directories")
                choose("Next page")
                evidence("split-next-page")
                choose("Search all indexed directories")
                box = page.locator(".quick-input-widget input").first
                act(box.fill, "level-21")
                act(page.keyboard.press, "Enter")
                page.get_by_text("deep/level-00/", exact=False).first.wait_for()
                evidence("split-deep-search")
                choose("deep/level-00/")
                choose("Review 1 explicit choices")
                evidence("split-review")
                choose("Finish read-only review")
                page.get_by_text("1 explicit directory choices reviewed.", exact=False).first.wait_for()
                evidence("split-finished")
            ledger["verdict"] = "PASS"
        except Exception as error:
            ledger["verdict"] = "FAIL"
            record("failure", error=str(error))
            evidence("failure")
            raise
        finally:
            ledger["actions"] = actions
            record("browser-cleanup")
            browser.close()
    return ledger


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--chrome", default="/usr/bin/google-chrome")
    parser.add_argument("--phase", choices=["activation", "discovery", "catalog", "inspect"], default="activation")
    args = parser.parse_args()
    if not args.url.startswith("http://127.0.0.1:"):
        parser.error("Only a task-owned loopback editor is supported")
    walk(args.url, args.output, args.chrome, args.phase)
