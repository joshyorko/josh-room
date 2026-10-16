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
let getDiagnostics;
let getFailureDiagnostics;
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
  receipt.vscode_version = process.env.CAPTURE_VSCODE_VERSION;
  if (!/^\d+\.\d+\.\d+/.test(receipt.vscode_version || "")) throw new Error("Actual VS Code CLI version is unavailable");
  receipt.vscode_executable_sha256 = sha(path.join(codeRoot, "Code.exe"));
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
  const sanitize = (value) => {
    let text = String(value);
    for (const known of [installedRoot, fixture, profile, extensions, codeRoot, temp, root].sort((a, b) => b.length - a.length)) {
      text = text.split(known).join("<disposable-path>");
      text = text.split(known.replaceAll("\\", "/")).join("<disposable-path>");
    }
    return text.replace(/[A-Za-z]:[\\/][^\s"'<>]*/g, "<windows-path>")
      .replace(/https?:\/\/[^\s"'<>]+/g, "<url>").slice(0, 600);
  };
  getFailureDiagnostics = () => {
    const result = { output_logs_examined: 0, output_log_bytes: 0, failure_lines: [],
      receipt_files: [], traversal_truncated: false, unreadable_entries: 0 };
    let entries = 0;
    const visit = (directory, depth, type) => {
      if (depth > 8 || entries >= 500) { result.traversal_truncated = true; return; }
      let children;
      try { children = fs.readdirSync(directory, { withFileTypes: true }); }
      catch { result.unreadable_entries++; return; }
      for (const entry of children) {
        if (++entries > 500) { result.traversal_truncated = true; break; }
        if (entry.isSymbolicLink()) continue;
        const file = path.join(directory, entry.name);
        if (entry.isDirectory()) { visit(file, depth + 1, type); continue; }
        if (!entry.isFile()) continue;
        try {
          if (type === "output" && /josh[ _-]*room.*\.log$/i.test(entry.name) && result.output_logs_examined < 8) {
            const size = fs.statSync(file).size;
            const length = Math.min(size, 65536);
            const buffer = Buffer.alloc(length);
            const fd = fs.openSync(file, "r");
            try { fs.readSync(fd, buffer, 0, length, size - length); } finally { fs.closeSync(fd); }
            result.output_logs_examined++;
            result.output_log_bytes += size;
            for (const line of buffer.toString("utf8").split(/\r?\n/)) {
              // Only already-sanitized extension RCC/controller diagnostics, never stdout or environment dumps.
              if (!/RCC stderr:|Unable to read (?:RCC|controller result) receipt|RCC did not produce a controller receipt/.test(line)) continue;
              if (/credential|password|secret|token|authorization|access.?key|private.?key/i.test(line)) continue;
              if (result.failure_lines.length < 24) result.failure_lines.push(sanitize(line));
            }
          }
          if (type === "runtime" && /(?:receipt|controller).*\.json$/i.test(entry.name) && result.receipt_files.length < 16) {
            // Receipt contents may contain environments: report only finite file metadata.
            result.receipt_files.push({ kind: /receipt/i.test(entry.name) ? "receipt" : "controller-json", bytes: fs.statSync(file).size });
          }
        } catch { result.unreadable_entries++; }
      }
    };
    visit(path.join(profile, "logs"), 0, "output");
    visit(path.join(profile, "User", "globalStorage", "joshyorko.josh-room", "logs"), 0, "runtime");
    return result;
  };
  const notifications = new Map();
  const captureNotifications = async () => {
    const messages = await evaluate(`Array.from(document.querySelectorAll('.notification-list-item-message, .monaco-dialog-message'))
      .filter(e => e.getBoundingClientRect().width > 0).slice(0, 12).map(e => e.innerText.slice(0, 2000))`);
    for (const raw of messages) {
      const message = sanitize(raw);
      if (!notifications.has(message) && notifications.size < 16) {
        const errorClass = /checksum|artifact|digest/i.test(message) ? "artifact" :
          /rcc|runtime|python|controller/i.test(message) ? "runtime-controller" :
          /network|download|connection/i.test(message) ? "network" :
          /error|fail|unable|cannot/i.test(message) ? "other-error" : "informational";
        notifications.set(message, { stage, message, error_class: errorClass });
      }
    }
    receipt.notifications = [...notifications.values()];
  };
  getDiagnostics = async () => {
    await captureNotifications();
    return evaluate(`(() => {
    const workbench = document.querySelector('.monaco-workbench');
    const palette = document.querySelector('.quick-input-widget input');
    return { ready_state: document.readyState, visibility: document.visibilityState,
      focused: document.hasFocus(), workbench_present: !!workbench,
      editor_present: !!document.querySelector('.part.editor'),
      palette_present: !!palette, palette_visible: !!palette && palette.getBoundingClientRect().width > 0,
      active_element: document.activeElement?.tagName || null };
  })()`);
  };
  await until(() => evaluate("document.readyState === 'complete' && !!document.querySelector('.monaco-workbench .part.editor')"), 60000, "loaded native workbench");
  await send("Page.bringToFront");
  await evaluate("window.focus()");
  receipt.renderer_ready = await getDiagnostics();
  const key = async (key, code, virtual, modifiers = 0) => {
    await send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: virtual, modifiers });
    await send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: virtual, modifiers });
  };
  const text = async () => { await captureNotifications(); return evaluate("document.body.innerText"); };
  const workspaceText = async () => {
    const value = await text();
    if (/RCC did not produce a controller receipt/.test(value)) {
      throw new Error("Native controller failed: RCC did not produce a controller receipt");
    }
    return value;
  };
  const command = async () => {
    await key("Escape", "Escape", 27);
    await send("Page.bringToFront");
    await evaluate("window.focus()");
    const point = await evaluate("(() => { const r = document.querySelector('.part.editor').getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()");
    await send("Input.dispatchMouseEvent", { type: "mousePressed", ...point, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...point, button: "left", clickCount: 1 });
    await key("F1", "F1", 112);
    await until(() => evaluate("!!Array.from(document.querySelectorAll('.quick-input-widget input')).find(e=>e.getBoundingClientRect().width>0)"), 15000, "native command palette");
    await key("a", "KeyA", 65, 2);
    await send("Input.insertText", { text: ">Josh: Check Workspace" });
    const row = await until(() => evaluate(`(() => {
      const rows = Array.from(document.querySelectorAll('.quick-input-widget .monaco-list-row'))
        .filter(e => e.getBoundingClientRect().width > 0 &&
          Array.from(e.querySelectorAll('.label-name, .monaco-highlighted-label'))
            .some(label => label.textContent.trim() === 'Josh: Check Workspace'));
      if (rows.length !== 1) return null;
      const r = rows[0].getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()`), 30000, "exact visible installed extension command row");
    receipt.command_selections ??= [];
    receipt.command_selections.push({ stage, label: "Josh: Check Workspace", exact_visible_rows: 1 });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", ...row, button: "left", clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", ...row, button: "left", clickCount: 1 });
  };
  stage = "source-only-native-check";
  await command();
  await until(async () => /Workspace valid [·•] \d+ entries/.test(await workspaceText()), 360000, "source-only native workspace check result");
  receipt.checks.push("native-command-source-only-valid");
  receipt.source_only_ui_result = (await text()).match(/Workspace valid [·•] \d+ entries/)[0];
  fs.writeFileSync(ignore, "# full-repository is the compatibility default\n");
  stage = "full-repository-native-check";
  await command();
  const gitResult = await until(async () => (await workspaceText()).match(/git-storage[^\n]*|Git storage[^\n]*/), 90000, "fresh full-repository Git blocker");
  receipt.full_repository_ui_result = gitResult[0].slice(0, 500);
  receipt.checks.push("native-command-policy-change-rejects-unsupported-git");
  if (fs.readFileSync(path.join(fixture, ".git"), "utf8") !== "gitdir: Z:/synthetic-unavailable-admin\n") throw new Error("Original fixture Git entry changed");
  receipt.checks.push("original-fixture-git-entry-unchanged");
  receipt.status = "passed";
}
main().catch(async (error) => {
  receipt.error = String(error.message).slice(0,500);
  if (getDiagnostics) { try { receipt.renderer_diagnostics = await getDiagnostics(); } catch {} }
  if (getFailureDiagnostics) { try { receipt.controller_failure_diagnostics = getFailureDiagnostics(); } catch { receipt.controller_failure_diagnostics = { extraction_failed: true }; } }
  process.exitCode = 1;
}).finally(() => {
  receipt.final_stage = stage;
  socket?.close();
  if (child?.pid) { try { cp.execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" }); } catch {} }
  const body = JSON.stringify(receipt, null, 2);
  console.log(body);
  if (process.env.CAPTURE_UI_ROOT) fs.writeFileSync(path.join(process.env.CAPTURE_UI_ROOT, "receipt.json"), body + "\n");
  if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `\n### Windows packaged native UI qualification\n\n\`\`\`json\n${body}\n\`\`\`\n`);
});
