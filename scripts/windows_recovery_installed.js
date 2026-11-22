"use strict";
// Ordinary CI only. Uses real Electron CDP key input and native VS Code widgets.
const fs = require("node:fs");
const fsp = fs.promises;
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");
const net = require("node:net");
const { spawn, spawnSync } = require("node:child_process");
const repository = path.resolve(__dirname, "..");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const digest = data => crypto.createHash("sha256").update(data).digest("hex");
const pins = require("./windows_recovery_dependencies.json");

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", timeout: 180000, ...options });
  if (result.error || result.status !== 0) {
    throw new Error(`Acceptance dependency failed: ${path.basename(command)} (exit ${result.status}, ${result.error?.code || result.signal || "no-signal"})`);
  }
  return result.stdout.trim();
}
async function download(pin, destination) {
  const response = await fetch(pin.url, { signal: AbortSignal.timeout(120000) });
  if (!response.ok) throw new Error(`Official dependency HTTP ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (digest(bytes) !== pin.sha256) throw new Error("Dependency checksum mismatch");
  await fsp.writeFile(destination, bytes, { flag: "wx" });
}
async function reservePort() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}
async function until(probe, label, timeout = 60000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await probe();
    if (value) return value;
    await delay(200);
  }
  throw new Error(`Bounded acceptance timed out: ${label}`);
}
async function connect(port) {
  const target = await until(async () => {
    try {
      const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      return targets.find(value => value.type === "page" && value.url.includes("workbench"));
    } catch { return null; }
  }, "native Electron workbench");
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  let id = 0;
  const pending = new Map();
  socket.onmessage = event => {
    const result = JSON.parse(event.data);
    if (pending.has(result.id)) { pending.get(result.id)(result); pending.delete(result.id); }
  };
  const send = async (method, params = {}) => {
    const request = ++id;
    const response = new Promise(resolve => pending.set(request, resolve));
    socket.send(JSON.stringify({ id: request, method, params }));
    const result = await Promise.race([response, delay(10000).then(() => { throw new Error("CDP command timeout"); })]);
    if (result.error) throw new Error("Native Electron CDP command failed");
    return result.result;
  };
  const evaluate = async expression => (await send("Runtime.evaluate", { expression, returnByValue: true })).result.value;
  const key = async (key, code, virtual, modifiers = 0) => {
    for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", {
      type, key, code, windowsVirtualKeyCode: virtual, modifiers,
    });
  };
  const text = async value => { await key("a", "KeyA", 65, 2); await send("Input.insertText", { text: value }); };
  const widget = () => evaluate(`(() => {const w=[...document.querySelectorAll('.quick-input-widget')].find(x=>x.offsetParent!==null);return w ? {text:w.innerText,rows:[...w.querySelectorAll('.monaco-list-row')].map(x=>x.innerText)}:null})()`);
  const choose = async label => {
    await until(async () => (await widget())?.rows.some(value => value.includes(label)), `native choice ${label}`);
    await text(label);
    await until(async () => (await widget())?.rows[0]?.includes(label), "filtered native choice");
    await key("Enter", "Enter", 13);
  };
  const command = async title => {
    await key("p", "KeyP", 80, 2 | 8);
    await text(">" + title);
    await until(async () => (await widget())?.rows.some(value => value.includes(title)), "registered driver command");
    await key("Enter", "Enter", 13);
  };
  return { socket, send, evaluate, key, text, widget, choose, command };
}

async function main() {
  if (process.platform !== "win32") throw new Error("Native Windows acceptance only");
  const python = process.env.JOSH_ROOM_ACCEPTANCE_PYTHON;
  if (!python) throw new Error("Existing project Python required");
  const base = await fsp.mkdtemp(path.join(os.tmpdir(), "josh-room-installed-recovery-"));
  const evidence = process.env.JOSH_ROOM_WINDOWS_RECOVERY_EVIDENCE;
  if (!evidence) throw new Error("Explicit sanitized evidence directory required");
  await fsp.mkdir(evidence, { recursive: true });
  let editor, provider, cdp;
  let failure = null;
  const receipt = { state: "FAIL", source_sha: process.env.GITHUB_SHA || "local",
    pr_head_sha: process.env.JOSH_ROOM_ACCEPTANCE_HEAD_SHA || "local",
    runtime: "installed-source-Python-fallback-not-immutable-RCC", storage: "real-loopback-MinIO",
    storage_and_native_custody_state: "NOT RUN",
    platform: process.platform, dependencies: pins, unproven: ["immutable RCC feature artifact", "other providers"] };
  try {
    const tools = path.join(base, "tools");
    await fsp.mkdir(tools);
    await download(pins.minio, path.join(tools, "minio.exe"));
    for (const name of ["age", "vscode"]) {
      const archive = path.join(base, name + ".zip");
      await download(pins[name], archive);
      await fsp.mkdir(path.join(tools, name));
      run("tar", ["-xf", archive, "-C", path.join(tools, name)]);
    }
    const code = path.join(tools, "vscode/Code.exe");
    const candidate = path.join(base, "candidate.vsix");
    run("npm.cmd", ["run", "package", "--", "--out", candidate], {
      cwd: path.join(repository, "vscode-extension"), shell: true,
    });
    receipt.vsix_sha256 = digest(await fsp.readFile(candidate));
    const profile = path.join(base, "profile");
    const extensions = path.join(base, "extensions");
    run("pwsh", ["-NoProfile", "-NonInteractive", "-File",
      path.join(__dirname, "install_windows_acceptance_vsix.ps1"),
      "-CodeRoot", path.join(tools, "vscode"), "-Profile", profile,
      "-Extensions", extensions, "-Candidate", candidate]);
    const installed = path.join(extensions, (await fsp.readdir(extensions)).find(name => name.startsWith("joshyorko.josh-room-")));
    // Compare every packaged source byte, excluding only installation metadata.
    const unpack = path.join(base, "unpack");
    await fsp.mkdir(unpack);
    run("tar", ["-xf", candidate, "-C", unpack]);
    let compared = 0;
    async function parity(directory, relative = "") {
      for (const entry of await fsp.readdir(directory, { withFileTypes: true })) {
        const rel = path.join(relative, entry.name);
        if (entry.isDirectory()) await parity(path.join(directory, entry.name), rel);
        else {
          const original = await fsp.readFile(path.join(directory, entry.name));
          const actual = await fsp.readFile(path.join(installed, rel));
          if (rel === "package.json") {
            const value = JSON.parse(actual); delete value.__metadata;
            if (JSON.stringify(value) !== JSON.stringify(JSON.parse(original))) throw new Error("Installed manifest mismatch");
          } else if (!actual.equals(original)) throw new Error("Installed VSIX byte parity failed");
          compared++;
        }
      }
    }
    await parity(path.join(unpack, "extension"));
    receipt.installed_files_compared = compared;
    const env = { ...process.env, JOSH_ROOM_ACCEPTANCE_ROOT: base,
      JOSH_ROOM_ACCEPTANCE_PORT: String(await reservePort()),
      PYTHONPATH: path.join(installed, "runtime/controller"),
      PATH: path.join(tools, "age/age") + path.delimiter + process.env.PATH };
    delete env.JOSH_ROOM_RESULT_FILE;
    delete env.JOSH_ROOM_CREDENTIAL_FILE;
    const providerErrors = fs.openSync(path.join(base, "provider-errors.log"), "w");
    provider = spawn(python, [path.join(__dirname, "windows_recovery_provider.py")], {
      env, stdio: ["ignore", "ignore", providerErrors],
    });
    fs.closeSync(providerErrors);
    await until(async () => {
      if (provider.exitCode !== null) throw new Error("Native provider setup failed");
      return fs.existsSync(path.join(base, "provider-ready.json"));
    }, "production Dimension/keyset/snapshots", 240000);
    receipt.storage_and_native_custody_state = "PASS";
    const driver = path.join(base, "driver");
    await fsp.mkdir(driver);
    await fsp.copyFile(path.join(__dirname, "windows_recovery_ui_driver.js"), path.join(driver, "extension.js"));
    await fsp.writeFile(path.join(driver, "package.json"), JSON.stringify({ name: "recovery-fixture",
      publisher: "synthetic", version: "0.0.1", engines: { vscode: "^1.90.0" },
      main: "extension.js", activationEvents: ["onStartupFinished"], contributes: { commands: [
        { command: "recoveryFixture.start", title: "Recovery fixture start" },
        { command: "recoveryFixture.verifyDiff", title: "Recovery fixture verify diff" },
      ] } }));
    await fsp.mkdir(path.join(profile, "User"), { recursive: true });
    await fsp.writeFile(path.join(profile, "User/settings.json"), JSON.stringify({
      "security.workspace.trust.enabled": false, "files.simpleDialog.enable": true,
      "telemetry.telemetryLevel": "off", "update.mode": "none", "extensions.autoUpdate": false,
      "workbench.startupEditor": "none", "window.restoreWindows": "none",
    }));
    const port = await reservePort();
    editor = spawn(code, ["--user-data-dir", profile, "--extensions-dir", extensions,
      "--extensionDevelopmentPath=" + driver, "--remote-debugging-port=" + port,
      "--remote-debugging-address=127.0.0.1", "--disable-gpu", "--skip-welcome", "--skip-release-notes",
      path.join(base, "provider-workspace")], { env, stdio: "ignore" });
    await until(() => fs.existsSync(path.join(base, "ui-driver-ready.json")), "installed extension activation", 90000);
    cdp = await connect(port);
    await cdp.command("Recovery fixture start");
    await cdp.choose("code.py");
    await cdp.choose("Compare with current workspace");
    await until(async () => (await cdp.widget())?.rows.some(value => value.includes("code.py")), "pinned tree after compare");
    await cdp.key("Escape", "Escape", 27);
    await cdp.command("Recovery fixture verify diff");
    await until(() => fs.existsSync(path.join(base, "ui-diff.json")), "native diff verification");
    if (JSON.parse(await fsp.readFile(path.join(base, "ui-diff.json"))).state !== "PASS") throw new Error("Native readonly text diff failed");
    await cdp.command("Recovery fixture start");
    await cdp.choose("code.py");
    await cdp.choose("Recover selection…");
    const destination = path.join(base, "recovered-one-file");
    await until(async () => (await cdp.widget())?.text.includes("Recover to a new"), "native save dialog");
    await cdp.text(destination);
    await cdp.key("Enter", "Enter", 13);
    await until(() => fs.existsSync(destination), "selective extraction through registered command");
    const expected = Buffer.from('def greeting():\n\treturn "first 你好"\n');
    if (!(await fsp.readFile(destination)).equals(expected)) throw new Error("Historical selective recovery bytes differ");
    await until(async () => (await cdp.widget())?.rows.some(value => value.includes("code.py")), "pinned tree after extraction");
    await cdp.key("Escape", "Escape", 27);
    const cancelled = await until(async () => {
      try {
        const result = JSON.parse(await fsp.readFile(path.join(base, "ui-command-finished.json")));
        return result.invocation === 2 && result.result === "cancelled" && result;
      } catch { return null; }
    }, "native picker cancellation");
    if (fs.existsSync(path.join(base, "ui-command-failed.json"))) throw new Error("Registered action failed");
    receipt.native_picker = "PASS";
    receipt.native_readonly_diff = "PASS";
    receipt.registered_selective_extract = "PASS";
    receipt.recovered_sha256 = digest(expected);
    receipt.cancelled_without_extra_writes = true;
    receipt.cancelled_exact_pin = cancelled.pin;
  } catch (error) { failure = error; receipt.failed_boundary = error.message; }
  finally {
    cdp?.socket.close();
    if (editor?.pid && editor.exitCode === null) {
      try { run("taskkill", ["/pid", String(editor.pid), "/T", "/F"]); }
      catch { failure ||= new Error("Owned editor cleanup failed"); }
    }
    if (provider) {
      await fsp.writeFile(path.join(base, "stop-provider"), "stop");
      await until(() => provider.exitCode !== null, "owned native credential cleanup", 30000);
      for (const name of ["provider-ready.json", "provider-failed.json", "ui-diff.json", "custody-cleanup.json", "preservation.json"]) {
        if (fs.existsSync(path.join(base, name))) {
          const value = JSON.parse(await fsp.readFile(path.join(base, name)));
          await fsp.writeFile(path.join(evidence, name), JSON.stringify(value, null, 2));
        }
      }
      if (provider.exitCode !== 0) { failure ||= new Error("Provider custody/preservation cleanup failed"); receipt.cleanup = "FAIL"; }
      if (fs.existsSync(path.join(base, "provider-errors.log"))) {
        // Only sanitized exception type/boundary; never copy raw provider or editor logs.
        receipt.provider_error_log_bytes = (await fsp.stat(path.join(base, "provider-errors.log"))).size;
      }
    }
    receipt.state = failure ? "FAIL" : "PASS";
    await fsp.writeFile(path.join(evidence, "installed-windows-recovery.json"), JSON.stringify(receipt, null, 2));
    await fsp.rm(base, { recursive: true, force: true });
  }
  if (failure) throw failure;
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { digest, pins };
