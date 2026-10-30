#!/usr/bin/env node
"use strict";
// Genuine workbench pixels and production delete handlers, with explicitly
// synthetic in-memory controller boundaries in a disposable candidate copy.
const fs = require("node:fs");
const path = require("node:path");
const cp = require("node:child_process");
const net = require("node:net");
const crypto = require("node:crypto");
const { isDeepStrictEqual } = require("node:util");
const root = path.resolve(__dirname, "..");
const ui = process.env.ISSUE165_UI_ROOT;
const codeRoot = process.env.ISSUE165_CODE_ROOT;
const vsix = process.env.ISSUE165_VSIX;
const fixtureRoot = process.env.ISSUE165_FIXTURE_ROOT;
const sha = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
const receipt = { gate: "issue-165-installed-vscode-delete-ui", status: "failed",
  evidence_level: "modified disposable candidate copy; real VS Code UI and delete handlers; in-memory controller boundaries",
  source_sha: cp.execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim(),
  vscode_version: process.env.ISSUE165_VSCODE_VERSION, checks: [], screenshots: [], provider_calls: "NONE" };
let child;
let socket;
let stage = "preflight";
let captureFailure;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(fn, timeout, label) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    if (child && child.exitCode !== null) throw new Error("VS Code exited during " + label);
    await delay(300);
  }
  throw new Error("Timed out waiting for " + label);
}
function verifyInstalledManifests(installedPath, fixturePath, sourcePath) {
  const installed = JSON.parse(fs.readFileSync(installedPath, "utf8"));
  const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
  const expected = JSON.parse(fs.readFileSync(sourcePath, "utf8"));
  // VS Code's extensionsScannerService.updateMetadata adds __metadata and
  // rewrites JSON formatting. All public fields must still match exactly.
  const { __metadata, ...publicManifest } = installed;
  if (!isDeepStrictEqual(publicManifest, expected)) throw new Error("Installed public manifest differs from exact source");
  if (!isDeepStrictEqual(fixture, expected)) throw new Error("Fixture public manifest differs from exact source");
  return Object.keys(__metadata || {}).sort();
}

async function main() {
  if (process.platform !== "win32" || !ui || !codeRoot || !vsix || !fixtureRoot) throw new Error("Missing isolated Windows fixture inputs");
  const identity = JSON.parse(fs.readFileSync(path.join(ui, "fixture-identity.json"), "utf8"));
  if (identity.candidate_vsix_sha256 !== sha(vsix)) throw new Error("Pristine VSIX changed after fixture preparation");
  receipt.vsix_sha256 = sha(vsix);
  receipt.extension_version = identity.extension_version;
  const installed = path.join(ui, "extensions", `joshyorko.josh-room-${identity.extension_version}`);
  for (const file of ["extension.js", "runtime/manifest.json"]) {
    if (sha(path.join(installed, file)) !== sha(path.join(root, "vscode-extension", file))) throw new Error("Pristine installed payload mismatch: " + file);
  }
  const fixtureExtension = path.join(fixtureRoot, identity.candidate_extension);
  receipt.installer_metadata_fields = verifyInstalledManifests(path.join(installed, "package.json"),
    path.join(fixtureExtension, "package.json"), path.join(root, "vscode-extension/package.json"));
  receipt.checks.push("pristine-installed-payload-matches-source", "fixture-keeps-public-command-manifest");
  const workspace = path.join(ui, "synthetic-workspace");
  const profile = path.join(ui, "fixture-profile");
  fs.mkdirSync(workspace, { recursive: true });
  fs.mkdirSync(path.join(profile, "User"), { recursive: true });
  fs.writeFileSync(path.join(workspace, "README.md"), "Disposable issue165 UI fixture. No provider or user data.\n");
  fs.writeFileSync(path.join(profile, "User/settings.json"), JSON.stringify({
    "security.workspace.trust.enabled": false, "workbench.startupEditor": "none", "window.dialogStyle": "custom",
    "window.titleBarStyle": "custom", "update.mode": "none", "extensions.autoUpdate": false, "telemetry.telemetryLevel": "off",
  }));
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  stage = "launch";
  child = cp.spawn(path.join(codeRoot, "Code.exe"), ["--new-window", "--disable-gpu", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes",
    `--remote-debugging-port=${port}`, "--user-data-dir", profile, "--extensions-dir", path.join(fixtureRoot, "extensions"), workspace], { stdio: "ignore" });
  const page = await until(async () => {
    try { return (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(row => row.type === "page" && /workbench/.test(row.url)); }
    catch { return undefined; }
  }, 90000, "real workbench CDP target");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    message.error ? waiter.reject(new Error(message.error.message)) : waiter.resolve(message.result);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error("CDP timeout: " + method)); }, 15000);
    pending.set(id, { resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true });
    if (result.exceptionDetails) throw new Error("Workbench inspection failed");
    return result.result.value;
  };
  const key = async (name, code, virtual, modifiers = 0) => {
    await send("Input.dispatchKeyEvent", { type: "keyDown", key: name, code, windowsVirtualKeyCode: virtual, modifiers });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key: name, code, windowsVirtualKeyCode: virtual, modifiers });
  };
  const click = async (box, button = "left") => {
    const point = { x: box.x + box.width / 2, y: box.y + box.height / 2, button, clickCount: 1 };
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", ...point });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...point });
  };
  const rowExpression = `Array.from(document.querySelectorAll('.monaco-list-row')).find(e => e.getBoundingClientRect().width > 0 && e.innerText.includes('Issue165 Synthetic Room'))`;
  const row = () => evaluate(`(() => { const e = ${rowExpression}; return e ? e.getBoundingClientRect().toJSON() : null; })()`);
  const state = () => { try { return JSON.parse(fs.readFileSync(path.join(fixtureRoot, "state.json"), "utf8")); } catch { return undefined; } };
  const screenshot = async (name, boxes, label) => {
    const result = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    fs.writeFileSync(path.join(ui, name + ".raw.png"), Buffer.from(result.data, "base64"));
    receipt.screenshots.push({ raw: name + ".raw.png", path: name + ".png", boxes, label, evidence_level: receipt.evidence_level });
  };
  const menuItem = async (label) => until(() => evaluate(`(() => {
    const e = Array.from(document.querySelectorAll('.monaco-menu .action-label')).find(e => e.getBoundingClientRect().width > 0 && e.textContent.trim().replace(/^Josh: /, '') === ${JSON.stringify(label)});
    if (!e || e.closest('[aria-disabled="true"], .disabled')) return null;
    // Context menus animate when opening. A transient rectangle can point at
    // dead space by the time the mouse arrives; wait for the real hit target.
    for (let parent = e; parent; parent = parent.parentElement) {
      if (parent.getAnimations().some(animation => animation.playState === 'running')) return null;
    }
    const box = e.getBoundingClientRect();
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return hit && (e.contains(hit) || hit.contains(e)) ? box.toJSON() : null;
  })()`), 15000, label + " settled menu action");
  captureFailure = async () => {
    receipt.renderer_failure = await evaluate(`(() => ({
      dialogs: Array.from(document.querySelectorAll('.monaco-dialog-box')).map(e=>e.innerText.slice(0,1000)),
      notifications: Array.from(document.querySelectorAll('.notification-list-item-message')).map(e=>e.innerText.slice(0,1000)),
      menus: Array.from(document.querySelectorAll('.monaco-menu .action-label')).map(e=>({text:e.textContent,disabled:!!e.closest('[aria-disabled="true"], .disabled')})),
      quick_input: Array.from(document.querySelectorAll('.quick-input-widget')).filter(e=>e.getBoundingClientRect().width>0).map(e=>e.innerText.slice(0,1000))
    }))()`);
    await screenshot("failure-" + stage, [], "Failed synthetic presentation stage; no acceptance claim");
  };
  const openRoomMenu = async () => { await click(await until(row, 15000, "Room row"), "right"); };
  const dialog = () => evaluate(`(() => { const e = Array.from(document.querySelectorAll('.monaco-dialog-box')).find(e => e.getBoundingClientRect().width > 0); return e ? {box:e.getBoundingClientRect().toJSON(),text:e.innerText} : null; })()`);
  const dialogButton = async (label) => until(() => evaluate(`(() => {
    const e = Array.from(document.querySelectorAll('.monaco-dialog-box button, .monaco-dialog-box .monaco-button')).find(e => e.getBoundingClientRect().width > 0 && e.textContent.trim().replaceAll('&','') === ${JSON.stringify(label)});
    return e ? e.getBoundingClientRect().toJSON() : null;
  })()`), 15000, label + " confirmation button");
  const expandRoom = async () => {
    const twistie = await evaluate(`(() => { const row = ${rowExpression}; if (!row || row.getAttribute('aria-expanded') === 'true') return null; const e=row.querySelector('.monaco-tl-twistie'); return e?.getBoundingClientRect().toJSON() || null; })()`);
    if (twistie) await click(twistie);
  };
  const recoveryRows = () => evaluate(`Array.from(document.querySelectorAll('.monaco-list-row')).filter(e => e.getBoundingClientRect().width > 0 && /Portable|Logical/.test(e.innerText)).map(e => ({box:e.getBoundingClientRect().toJSON(),text:e.innerText}))`);
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await until(() => evaluate("document.readyState === 'complete' && !!document.querySelector('.monaco-workbench')"), 60000, "loaded workbench");
  await send("Page.bringToFront");
  await evaluate("window.focus()");
  await until(() => state()?.initialized, 60000, "explicit in-memory catalog initialization");
  receipt.initial_state = state();
  if (receipt.initial_state.snapshot_ids.length !== 2 || receipt.initial_state.mutations.length) throw new Error("Fixture activation unexpectedly mutated the catalog");
  stage = "inline-trash";
  const roomBox = await until(row, 30000, "synthetic Room row");
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: roomBox.x + roomBox.width / 2, y: roomBox.y + roomBox.height / 2 });
  const trash = await until(() => evaluate(`(() => { const row=${rowExpression}; const e=Array.from(row?.querySelectorAll('[aria-label], [title]') || []).find(e => /Delete Snapshot/.test((e.getAttribute('aria-label')||'')+' '+(e.getAttribute('title')||'')) && e.getBoundingClientRect().width>0); return e?.getBoundingClientRect().toJSON()||null; })()`), 15000, "inline Delete Snapshot trash");
  await screenshot("01-inline-trash", [trash], "1  Delete Snapshot hover action; synthetic catalog");
  receipt.checks.push("inline-delete-snapshot-visible");
  stage = "keyboard-command";
  for (const title of ["Josh: Delete Room", "Josh: Delete Snapshot", "Josh: Delete Recovery Point"]) {
    const editor = await evaluate("document.querySelector('.part.editor').getBoundingClientRect().toJSON()");
    await click(editor);
    await key("F1", "F1", 112);
    await until(() => evaluate("!!Array.from(document.querySelectorAll('.quick-input-widget input')).find(e=>e.getBoundingClientRect().width>0)"), 15000, "command palette");
    await key("a", "KeyA", 65, 2);
    await send("Input.insertText", { text: ">" + title });
    await until(() => evaluate(`Array.from(document.querySelectorAll('.quick-input-widget .label-name')).some(e=>e.getBoundingClientRect().width>0&&e.textContent.trim()===${JSON.stringify(title)})`), 15000, "named " + title + " command");
    await key("Escape", "Escape", 27);
  }
  receipt.checks.push("all-delete-commands-keyboard-discoverable");
  stage = "room-confirmation-cancel";
  await openRoomMenu();
  const deleteRoom = await menuItem("Delete Room");
  await screenshot("03-delete-room-context", [deleteRoom], "3  Separate Delete Room context action; synthetic catalog");
  await click(deleteRoom);
  const roomDialog = await until(dialog, 15000, "Delete Room warning");
  if (!["issue165-room", "issue165-dimension", "2 recovery points", "irreversible"].every(value => roomDialog.text.includes(value))) throw new Error("Delete Room confirmation omitted its exact scope");
  await screenshot("04-room-confirmation", [roomDialog.box], "4  Genuine confirmation; cancel only; synthetic catalog");
  await key("Escape", "Escape", 27);
  await until(async () => !(await dialog()), 15000, "dismissed Room warning");
  if (state().mutations.length || state().snapshot_ids.length !== 2) throw new Error("Cancellation mutated the fixture");
  receipt.checks.push("room-confirmation-exact-scope", "cancel-zero-mutations");
  stage = "recovery-point-delete";
  await expandRoom();
  const portable = await until(async () => (await recoveryRows()).find(row => row.text.includes("Portable")), 15000, "portable recovery point row");
  await click(portable.box, "right");
  const portableDelete = await menuItem("Delete Recovery Point");
  await screenshot("08-portable-recovery-context", [portableDelete], "8  Portable recovery point context; synthetic catalog");
  await key("Escape", "Escape", 27);
  receipt.checks.push("portable-recovery-point-context");
  const latest = await until(async () => (await recoveryRows()).find(row => row.text.includes("Latest") && row.text.includes("Logical")), 15000, "Latest logical recovery point row");
  await click(latest.box, "right");
  const deletePoint = await menuItem("Delete Recovery Point");
  await screenshot("02-recovery-context", [deletePoint], "2  Scoped Delete Recovery Point; synthetic catalog");
  await click(deletePoint);
  const pointDialog = await until(dialog, 15000, "recovery-point warning");
  if (!["fixture-jat-new", "issue165-dimension"].every(value => pointDialog.text.includes(value))) throw new Error("Recovery point confirmation lost the clicked target");
  await screenshot("05-point-confirmation", [pointDialog.box], "5  Production confirmation; fixture-only mutation follows");
  await click(await dialogButton("Delete Snapshot"));
  await until(() => state()?.mutations.length === 1 && state().reloads >= 1, 15000, "production handler reload after fixture deletion");
  if (state().mutations[0].operation !== "snapshots remove" || state().mutations[0].snapshot_id !== "fixture-jat-new"
    || state().latest !== "fixture-jat-old" || state().snapshot_ids.length !== 1) throw new Error("Fixture deletion scope or Latest promotion is wrong");
  await expandRoom();
  await until(async () => { const rows = await recoveryRows(); return rows.length === 1 && rows[0].text.includes("Latest"); }, 15000, "fresh one-recovery-point tree");
  await screenshot("06-after-point-delete", [await row()], "6  Actual handler reload; synthetic controller state only");
  receipt.checks.push("scoped-point-confirmation", "one-point-mutation", "latest-promoted", "authoritative-handler-reload");
  stage = "whole-room-delete";
  await openRoomMenu();
  await click(await menuItem("Delete Room"));
  const finalDialog = await until(dialog, 15000, "remaining Room confirmation");
  if (!finalDialog.text.includes("1 recovery point")) throw new Error("Room count did not update after point deletion");
  await click(await dialogButton("Delete Room"));
  await until(() => state()?.mutations.length === 2 && state().reloads >= 2, 15000, "whole-Room fixture mutation and reload");
  await until(async () => !(await row()), 15000, "removed Room row");
  const sidebar = await evaluate("document.querySelector('.part.sidebar').getBoundingClientRect().toJSON()");
  await screenshot("07-after-room-delete", [sidebar], "7  Room removed by production handler; synthetic storage only");
  receipt.final_state = state();
  if (receipt.final_state.mutations[1].operation !== "rooms remove" || receipt.final_state.snapshot_ids.length) throw new Error("Whole-Room command used an unexpected mutation");
  if (sha(vsix) !== receipt.vsix_sha256) throw new Error("Pristine VSIX changed during UI qualification");
  receipt.checks.push("whole-room-operation-distinct", "room-row-disappears", "pristine-vsix-unchanged");
  receipt.status = "passed";
}
if (require.main === module) main().catch(async error => {
  if (captureFailure) { try { await captureFailure(); } catch {} }
  receipt.error = String(error.message).replace(/[A-Za-z]:[\\/][^\s"'<>]*/g, "<disposable-path>").slice(0, 500);
  process.exitCode = 1;
}).finally(() => {
  receipt.final_stage = stage;
  socket?.close();
  if (child?.pid) { try { cp.execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" }); } catch {} }
  const body = JSON.stringify(receipt, null, 2);
  console.log(body);
  if (ui) fs.writeFileSync(path.join(ui, "receipt.json"), body + "\n");
  if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, "\n### Issue165 synthetic presentation qualification\n\n```json\n" + body + "\n```\n");
});

module.exports = { verifyInstalledManifests };
