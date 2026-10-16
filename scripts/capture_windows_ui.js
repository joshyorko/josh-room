#!/usr/bin/env node
"use strict";
// Candidate-only renderer qualification. No screenshots, providers, or live data.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const cp = require("node:child_process");
const net = require("node:net");
const root = path.resolve(__dirname, "..");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const sha = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
const receipt = { gate: "packaged-windows-native-check-workspace", status: "failed", checks: [], full_save_restore: "NOT RUN", screenshots: "NOT CREATED" };
let child;
let socket;
let stage = "preflight";
async function until(fn, timeout, label) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    if (child && child.exitCode !== null) throw new Error("VS Code process exited before qualification completed");
    await sleep(500);
  }
  throw new Error(`Timed out: ${label}`);
}
async function main() {
  if (process.platform !== "win32") throw new Error("This qualification requires a native Windows runner");
  const temp = process.env.CAPTURE_UI_ROOT;
  const codeRoot = process.env.CAPTURE_CODE_ROOT;
  const vsix = process.env.CAPTURE_VSIX;
  if (!temp || !codeRoot || !vsix) throw new Error("Missing candidate harness inputs");
  const profile = path.join(temp, "profile");
  const extensions = path.join(temp, "extensions");
  const fixture = path.join(temp, "fixture");
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "vscode-extension/package.json")));
  receipt.source_sha = cp.execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
  receipt.vsix_sha256 = sha(vsix);
  receipt.extension_version = pkg.version;
  receipt.vscode_version = JSON.parse(fs.readFileSync(path.join(codeRoot, "resources/app/package.json"))).version;
  const installed = fs.readdirSync(extensions).filter((name) => name.toLowerCase() === `joshyorko.josh-room-${pkg.version}`.toLowerCase());
  if (installed.length !== 1) throw new Error("Installed candidate extension identity mismatch");
  const installedRoot = path.join(extensions, installed[0]);
  for (const relative of ["extension.js", "runtime.js", "dirty.js", "runtime/controller/josh_room/workspace_policy.py", "runtime/controller/josh_room/workspace_report.py"]) {
    if (sha(path.join(installedRoot, relative)) !== sha(path.join(root, "vscode-extension", relative))) throw new Error(`Installed payload mismatch: ${relative}`);
  }
  receipt.checks.push("installed-candidate-payload-matches-source");
  fs.mkdirSync(fixture, { recursive: true });
  fs.mkdirSync(path.join(profile, "User"), { recursive: true });
  fs.writeFileSync(path.join(profile, "User/settings.json"), JSON.stringify({ "security.workspace.trust.enabled": false, "workbench.startupEditor": "none", "update.mode": "none", "extensions.autoUpdate": false, "telemetry.telemetryLevel": "off" }));
  fs.writeFileSync(path.join(fixture, "selected.txt"), "synthetic selected project bytes\n");
  fs.writeFileSync(path.join(fixture, ".git"), "gitdir: Z:/synthetic-unavailable-admin\n");
  const ignore = path.join(fixture, ".josh-roomignore");
  fs.writeFileSync(ignore, "# josh-room capture-mode: source-only\n");
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  stage = "launch-renderer";
  child = cp.spawn(path.join(codeRoot, "Code.exe"), ["--new-window", "--disable-gpu", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes", "--remote-debugging-port=" + port, "--user-data-dir", profile, "--extensions-dir", extensions, fixture], { stdio: "ignore" });
  child.on("error", (error) => { receipt.launch_error = error.code; });
  const page = await until(async () => {
    try {
      const rows = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      return rows.find((row) => row.type === "page" && /workbench/.test(row.url));
    } catch { return null; }
  }, 90000, "native Windows VS Code renderer CDP availability");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener("message", ({ data }) => {
    const row = JSON.parse(data);
    const waiter = pending.get(row.id);
    if (waiter) { pending.delete(row.id); row.error ? waiter.reject(new Error(row.error.message)) : waiter.resolve(row.result); }
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve: (value) => { clearTimeout(timer); resolve(value); }, reject: (error) => { clearTimeout(timer); reject(error); } });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true });
    if (result.exceptionDetails) throw new Error("Renderer evaluation failed");
    return result.result.value;
  };
  const key = async (key, code, virtual, modifiers = 0) => {
    await send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: virtual, modifiers });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: virtual, modifiers });
  };
  const text = () => evaluate("document.body.innerText");
  const command = async () => {
    await key("Escape", "Escape", 27);
    await key("P", "KeyP", 80, 10);
    await until(() => evaluate("!!Array.from(document.querySelectorAll('.quick-input-widget input')).find(e=>e.getBoundingClientRect().width>0)"), 15000, "native command palette");
    await key("a", "KeyA", 65, 2);
    await send("Input.insertText", { text: ">Josh: Check Workspace" });
    await until(async () => /Josh: Check Workspace/.test(await text()), 20000, "installed extension command registration");
    await sleep(1000);
    await key("Enter", "Enter", 13);
  };
  stage = "source-only-native-check";
  await command();
  await until(async () => /Workspace valid [·•] \d+ entries/.test(await text()), 360000, "source-only native workspace check result");
  receipt.checks.push("native-command-source-only-valid");
  receipt.source_only_ui_result = (await text()).match(/Workspace valid [·•] \d+ entries/)[0];
  fs.writeFileSync(ignore, "# full-repository is the compatibility default\n");
  stage = "full-repository-native-check";
  await command();
  const gitResult = await until(async () => (await text()).match(/git-storage[^\n]*|Git storage[^\n]*/), 90000, "fresh full-repository Git blocker");
  receipt.full_repository_ui_result = gitResult[0].slice(0, 500);
  receipt.checks.push("native-command-policy-change-rejects-unsupported-git");
  if (fs.readFileSync(path.join(fixture, ".git"), "utf8") !== "gitdir: Z:/synthetic-unavailable-admin\n") throw new Error("Original fixture Git entry changed");
  receipt.checks.push("original-fixture-git-entry-unchanged");
  receipt.status = "passed";
}
main().catch((error) => { receipt.error = String(error.message).slice(0,500); process.exitCode = 1; }).finally(() => {
  receipt.final_stage = stage;
  socket?.close();
  if (child?.pid) { try { cp.execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" }); } catch {} }
  const body = JSON.stringify(receipt, null, 2);
  console.log(body);
  if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `\n### Windows packaged native UI qualification\n\n\`\`\`json\n${body}\n\`\`\`\n`);
});
