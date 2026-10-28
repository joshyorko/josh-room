#!/usr/bin/env node
"use strict";
// Bounded genuine-UI capability attempt. It writes screenshots only after the
// installed VSIX renders the expected synthetic Room and delete controls.
const fs = require("node:fs");
const path = require("node:path");
const cp = require("node:child_process");
const net = require("node:net");

const root = path.resolve(__dirname, "..");
const ui = process.env.ISSUE165_UI_ROOT;
const codeRoot = process.env.ISSUE165_CODE_ROOT;
const vsix = process.env.ISSUE165_VSIX;
const receipt = { gate: "issue-165-installed-vscode-delete-ui", status: "blocked", source_sha: cp.execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim(), vscode_version: process.env.ISSUE165_VSCODE_VERSION, screenshots: [] };
let child;
let socket;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  if (process.platform !== "win32" || !ui || !codeRoot || !vsix) throw new Error("Expected the isolated Windows runner inputs");
  const extensions = path.join(ui, "extensions");
  const installed = fs.readdirSync(extensions).filter((name) => /^joshyorko\.josh-room-/i.test(name));
  if (installed.length !== 1) throw new Error("Installed candidate VSIX identity is missing or ambiguous");
  const workspace = path.join(ui, "synthetic-workspace");
  fs.mkdirSync(workspace, { recursive: true });
  fs.writeFileSync(path.join(workspace, "README.md"), "Disposable issue-165 UI fixture. No provider or user data.\n");
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  child = cp.spawn(path.join(codeRoot, "Code.exe"), ["--new-window", "--disable-gpu", "--disable-workspace-trust", "--skip-welcome", "--skip-release-notes", `--remote-debugging-port=${port}`, "--user-data-dir", path.join(ui, "profile"), "--extensions-dir", extensions, workspace], { stdio: "ignore" });
  const deadline = Date.now() + 90000;
  let page;
  while (Date.now() < deadline) {
    try {
      const rows = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      page = rows.find((row) => row.type === "page" && /workbench/.test(row.url));
      if (page) break;
    } catch {}
    if (child.exitCode !== null) throw new Error("Code.exe exited before CDP connected");
    await delay(500);
  }
  if (!page) throw new Error("Timed out waiting for the real VS Code workbench CDP target");
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener("open", resolve, { once: true }); socket.addEventListener("error", reject, { once: true }); });
  let id = 0;
  const pending = new Map();
  socket.addEventListener("message", ({ data }) => {
    const message = JSON.parse(data);
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    message.error ? waiter.reject(new Error(message.error.message)) : waiter.resolve(message.result);
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const requestId = ++id;
    const timer = setTimeout(() => { pending.delete(requestId); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(requestId, { resolve: (result) => { clearTimeout(timer); resolve(result); }, reject });
    socket.send(JSON.stringify({ id: requestId, method, params }));
  });
  const evaluate = async (expression) => {
    const result = await send("Runtime.evaluate", { expression, returnByValue: true });
    if (result.exceptionDetails) throw new Error("Workbench DOM inspection failed");
    return result.result.value;
  };
  const screenshot = async (name) => {
    const result = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    const destination = path.join(ui, `${name}.png`);
    fs.writeFileSync(destination, Buffer.from(result.data, "base64"));
    return path.basename(destination);
  };
  await send("Page.enable");
  await send("Runtime.enable");
  await delay(12000);
  receipt.workbench = await evaluate(`(() => ({ ready: document.readyState, treeRows: Array.from(document.querySelectorAll('.monaco-list-row')).filter(e=>e.getBoundingClientRect().width>0).map(e=>e.innerText.trim().replace(/[A-Za-z]:\\\\[^\\s]*/g,'<path>').slice(0,120)).filter(Boolean).slice(0,40) }))()`);
  const roomRows = receipt.workbench.treeRows.filter((text) => /synthetic.*room|issue.?165.*room/i.test(text));
  if (!roomRows.length) throw new Error("Real VS Code and the installed extension launched, but no synthetic Room/catalog fixture is available to render deletion controls; refusing to capture unrelated or empty UI.");
  receipt.screenshots.push(await screenshot("room-tree"));
  const actions = await evaluate(`Array.from(document.querySelectorAll('body *')).filter(e=>e.children.length===0&&e.getBoundingClientRect().width>0).map(e=>e.innerText.trim()).filter(t=>t==='Delete Recovery Point'||t==='Delete Room')`);
  if (!actions.includes("Delete Recovery Point") || !actions.includes("Delete Room")) throw new Error("The real workbench did not expose both deletion menu actions for the synthetic Room");
  throw new Error("Synthetic deletion UI detected, but the capture path has not verified confirmations and post-delete reload; screenshots are withheld until all required states are validated");
}

main().catch((error) => {
  receipt.error = String(error.message).slice(0, 500);
  process.exitCode = 1;
}).finally(() => {
  socket?.close();
  if (child?.pid) { try { cp.execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" }); } catch {} }
  const body = JSON.stringify(receipt, null, 2);
  console.log(body);
  if (ui) fs.writeFileSync(path.join(ui, "receipt.json"), `${body}\n`);
  if (process.env.GITHUB_STEP_SUMMARY) fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, `\n### Issue 165 installed UI attempt\n\n\`\`\`json\n${body}\n\`\`\`\n`);
});
