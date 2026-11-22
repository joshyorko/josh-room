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
const { runCandidatePhase } = require("./installed_recovery_runtime");

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
  const widget = () => evaluate(`(() => {const w=[...document.querySelectorAll('.quick-input-widget')].find(x=>x.offsetParent!==null);return w ? {text:w.innerText,rows:[...w.querySelectorAll('.monaco-list-row')].map(x=>x.innerText),activeRows:w.querySelectorAll('.monaco-list-row.focused').length,input:w.querySelector('input')?.value,busy:[...w.querySelectorAll('.monaco-progress-container.infinite,.monaco-progress-container.discrete')].some(x=>x.offsetParent!==null)}:null})()`);
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
  const candidateFile = process.env.JOSH_ROOM_ACCEPTANCE_RCC_CANDIDATE;
  const candidateInput = candidateFile ? JSON.parse(await fsp.readFile(candidateFile, "utf8")) : null;
  if (!python && !candidateInput) throw new Error("Existing project Python or explicit RCC candidate required");
  if (candidateInput && (!/^[0-9a-f]{40}$/.test(candidateInput.subject_sha || "")
    || !/^sha256:[0-9a-f]{64}$/.test(candidateInput.artifact || "")
    || ![candidateInput.vsix_sha256, candidateInput.archive_sha256].every(x => /^[0-9a-f]{64}$/.test(x || "")))) {
    throw new Error("Invalid explicit candidate subject or input hashes");
  }
  const base = await fsp.mkdtemp(path.join(os.tmpdir(), "josh-room-installed-recovery-"));
  const evidence = process.env.JOSH_ROOM_WINDOWS_RECOVERY_EVIDENCE;
  if (!evidence) throw new Error("Explicit sanitized evidence directory required");
  await fsp.mkdir(evidence, { recursive: true });
  let editor, provider, cdp, fixtureEnv;
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
    if (candidateInput) {
      if (digest(await fsp.readFile(candidateInput.vsix)) !== candidateInput.vsix_sha256
        || digest(await fsp.readFile(candidateInput.archive)) !== candidateInput.archive_sha256) throw new Error("Candidate input checksum mismatch");
      await fsp.copyFile(candidateInput.vsix, candidate);
      receipt.subject_sha = candidateInput.subject_sha;
      receipt.runtime = "unpublished-immutable-RCC-candidate-installed-controller";
      receipt.unproven = ["published default runtime bootstrap", "other providers", "integrated release acceptance"];
    } else {
      run("npm.cmd", ["run", "package", "--", "--out", candidate], {
        cwd: path.join(repository, "vscode-extension"), shell: true,
      });
    }
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
      LOCALAPPDATA: path.join(base, "local-appdata"), APPDATA: path.join(base, "appdata"),
      TEMP: path.join(base, "provider-runtime"), TMP: path.join(base, "provider-runtime"),
      PATH: path.join(tools, "age/age") + path.delimiter + process.env.PATH };
    fixtureEnv = env;
    delete env.JOSH_ROOM_RESULT_FILE;
    delete env.JOSH_ROOM_CREDENTIAL_FILE;
    let providerCommand = python;
    let providerPrefix = [];
    if (candidateInput) {
      await fsp.mkdir(path.join(base, "provider-runtime"));
      const runtime = require(path.join(installed, "runtime.js"));
      const context = { globalStorageUri: { fsPath: path.join(base, "managed") } };
      const rcc = await runtime.ensureManagedRcc(context, runtime.readManifest());
      const rccHome = runtime.privatePaths(context).rccHome;
      if (!rccHome.startsWith(base + path.sep)) throw new Error("Candidate RCC storage escaped owned fixture");
      Object.assign(env, { ROBOCORP_HOME: rccHome, RCC_HOLOTREE_MODE: "private",
        JOSH_ROOM_RCC_EXE: rcc.executable, JOSH_ROOM_RCC_HOME: rccHome,
        JOSH_ROOM_CONTROLLER_ROOT: env.PYTHONPATH, JOSH_ROOM_CONTROLLER_ARTIFACT: candidateInput.artifact,
        JOSH_ROOM_JAT_ROOT: path.join(base, "empty-jat-log-root") });
      const record = event => {
        receipt.rcc_phases ||= {};
        receipt.rcc_phases[event.phase] = event;
        fs.writeFileSync(path.join(evidence, "installed-windows-recovery.json"), JSON.stringify(receipt, null, 2));
      };
      const acquired = runtime.parseJsonOutput(runCandidatePhase("acquire-archive", rcc.executable,
        ["env", "acquire", "--archive", candidateInput.archive, "--permissive-local", "--json"], { env, record }));
      if ((acquired.artifactDigest || acquired.artifact_digest) !== candidateInput.artifact
        || acquired.verification?.valid !== true) throw new Error("RCC candidate acquisition failed verification");
      const prefix = ["--no-build", "env", "exec", "--artifact", candidateInput.artifact, "--permissive-local", "--inherit-streams", "--"];
      const probe = JSON.parse(runCandidatePhase("interpreter-probe", rcc.executable, [...prefix, "python", "-c",
        "import json,sys,josh_room; print(json.dumps({'prefix':sys.prefix,'executable':sys.executable,'controller':josh_room.__file__}))"], { env, record }));
      if (!path.resolve(probe.executable).toLowerCase().startsWith(path.resolve(rccHome).toLowerCase() + path.sep)
        || !path.resolve(probe.controller).toLowerCase().startsWith(path.resolve(env.PYTHONPATH).toLowerCase() + path.sep)) {
        throw new Error("Candidate interpreter or controller origin mismatch");
      }
      env.JOSH_ROOM_ACCEPTANCE_RCC = JSON.stringify({ command: rcc.executable, artifact: candidateInput.artifact,
        subject_sha: candidateInput.subject_sha });
      receipt.rcc_candidate = { artifact: candidateInput.artifact, archive_sha256: candidateInput.archive_sha256,
        rcc_sha256: await runtime.sha256File(rcc.executable), interpreter_inside_private_RCC: true, installed_controller: true };
      const runtimeEnv = Object.fromEntries(["ROBOCORP_HOME", "RCC_HOLOTREE_MODE", "JOSH_ROOM_RCC_EXE", "JOSH_ROOM_RCC_HOME",
        "JOSH_ROOM_CONTROLLER_ROOT", "JOSH_ROOM_CONTROLLER_ARTIFACT", "JOSH_ROOM_JAT_ROOT"].map(key => [key, env[key]]));
      await fsp.writeFile(path.join(base, "candidate-runtime.json"), JSON.stringify({ ...JSON.parse(env.JOSH_ROOM_ACCEPTANCE_RCC), env: runtimeEnv }));
      providerCommand = rcc.executable;
      providerPrefix = [...prefix, "python"];
    }
    const providerErrors = fs.openSync(path.join(base, "provider-errors.log"), "w");
    provider = spawn(providerCommand, [...providerPrefix, path.join(__dirname, "windows_recovery_provider.py")], {
      env, stdio: ["ignore", "ignore", providerErrors],
    });
    fs.closeSync(providerErrors);
    await until(async () => {
      if (provider.exitCode !== null) throw new Error("Native provider setup failed");
      return fs.existsSync(path.join(base, "provider-ready.json"));
    }, "production Dimension/keyset/snapshots", candidateInput ? 480000 : 240000);
    receipt.storage_and_native_custody_state = "PASS";
    const providerReady = JSON.parse(await fsp.readFile(path.join(base, "provider-ready.json")));
    if (providerReady.history_save_pilot !== "unavailable-on-Windows") throw new Error("Windows history Save availability was not verified");
    receipt.history_save_pilot = "unavailable-on-Windows-actual-CLI-status-not-Save-UI";
    const inputDenials = JSON.parse(await fsp.readFile(path.join(base, "explicit-input-denials.json")));
    if (inputDenials.state !== "PASS" || inputDenials.records.length !== 4) throw new Error("Direct installed CLI input denials incomplete");
    receipt.direct_installed_cli_missing_deleted_handoff_denials = "PASS";
    const driver = path.join(base, "driver");
    await fsp.mkdir(driver);
    await fsp.copyFile(path.join(__dirname, "windows_recovery_ui_driver.js"), path.join(driver, "extension.js"));
    await fsp.copyFile(path.join(__dirname, "installed_recovery_runtime.js"), path.join(driver, "installed_recovery_runtime.js"));
    await fsp.copyFile(path.join(__dirname, "windows_recovery_controller.py"), path.join(base, "windows_recovery_controller.py"));
    await fsp.writeFile(path.join(driver, "package.json"), JSON.stringify({ name: "recovery-fixture",
      publisher: "synthetic", version: "0.0.1", engines: { vscode: "^1.90.0" },
      main: "extension.js", activationEvents: ["onStartupFinished"], contributes: { commands: [
        { command: "recoveryFixture.start", title: "Recovery fixture start" },
        { command: "recoveryFixture.focusDiff", title: "Recovery fixture focus diff" },
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
    const recoveryPins = JSON.parse(await fsp.readFile(path.join(base, "provider-pins.json")));
    const startBrowse = async invocation => {
      await cdp.command("Recovery fixture start");
      await until(async () => {
        try { return JSON.parse(await fsp.readFile(path.join(base, "ui-command-started.json"))).invocation === invocation; }
        catch { return false; }
      }, "registered start invocation");
      await until(async () => {
        if (fs.existsSync(path.join(base, "ui-command-failed.json"))) throw new Error("Registered recovery command failed before tree");
        const widget = await cdp.widget();
        if (widget?.rows.some(value => value.includes(recoveryPins[0]))) {
          await cdp.choose(recoveryPins[0]);
          receipt.native_exact_point_chooser = "PASS";
          return false;
        }
        return widget?.rows.some(value => value.includes("code.py"));
      }, "registered pinned file tree");
    };
    await startBrowse(1);
    await cdp.choose("code.py");
    await cdp.choose("Compare with current workspace");
    await until(async () => (await cdp.widget())?.rows.some(value => value.includes("code.py")), "pinned tree after compare");
    await cdp.key("Escape", "Escape", 27);
    await cdp.command("Recovery fixture focus diff");
    await until(() => fs.existsSync(path.join(base, "ui-editor-focus.json")), "native editor focus receipt");
    if (JSON.parse(await fsp.readFile(path.join(base, "ui-editor-focus.json"))).state !== "PASS") throw new Error("Native diff focus failed");
    await cdp.send("Input.insertText", { text: "synthetic forbidden edit" });
    await cdp.command("Recovery fixture verify diff");
    await until(() => fs.existsSync(path.join(base, "ui-diff.json")), "native diff verification");
    if (JSON.parse(await fsp.readFile(path.join(base, "ui-diff.json"))).state !== "PASS") throw new Error("Native readonly text diff failed");
    receipt.native_readonly_diff = "PASS";
    await startBrowse(2);
    await cdp.choose("code.py");
    await cdp.choose("Recover selection…");
    const destination = path.join(base, "recovered-one-file");
    await until(async () => {
      const widget = await cdp.widget();
      return widget?.text.includes("Recover to a new") && !widget.busy && widget.rows.length > 0;
    }, "native save dialog initial listing");
    // The simple dialog navigates directories asynchronously. Settle its parent
    // listing first; otherwise Enter may only navigate, without accepting a file.
    const parent = path.dirname(destination) + path.sep;
    const samePath = (left, right) => typeof left === "string"
      && path.normalize(left).toLowerCase() === path.normalize(right).toLowerCase();
    await cdp.text(parent);
    await until(async () => {
      const widget = await cdp.widget();
      return samePath(widget?.input, parent) && !widget.busy
        && widget.rows.some(row => row.includes("provider-workspace"));
    }, "native save dialog destination parent");
    await cdp.text(destination);
    await until(async () => {
      const widget = await cdp.widget();
      return samePath(widget?.input, destination) && !widget.busy && widget.activeRows === 0;
    }, "native save dialog absent filename");
    receipt.native_save_dialog_parent_navigation = "PASS";
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
    const proofs = (await fsp.readFile(path.join(base, "existing-handoff-probes.jsonl"), "utf8"))
      .trim().split("\n").map(line => JSON.parse(line));
    if (!["tree", "diff", "extract"].every(action => proofs.some(proof => proof.action === action
      && proof.native_private_existing_handoff && proof.cli_exit_status === 0))) throw new Error("Registered existing handoff proof incomplete");
    receipt.registered_existing_private_handoffs = { state: "PASS", operations: ["tree", "diff", "extract"], verified_calls: proofs.length };
  } catch (error) {
    failure = error; receipt.failed_boundary = error.message;
    if (cdp) {
      try {
        const widget = await cdp.widget();
        receipt.native_widget = { present: Boolean(widget), row_count: widget?.rows.length || 0,
          known_choices: ["code.py", "nested", "Recovery fixture start", "Compare with current workspace", "Recover selection"]
            .filter(value => widget?.rows.some(row => row.includes(value))),
          text_sha256: widget ? digest(widget.text) : null };
        receipt.native_surfaces = await cdp.evaluate(`(() => ({
          dialog_count: document.querySelectorAll('.monaco-dialog-box').length,
          notification_count: document.querySelectorAll('.notification-list-item').length,
          known_categories: ['encryption','authorization','private','runtime','password','keyring','catalog','Python','path','provider']
            .filter(value => [...document.querySelectorAll('.monaco-dialog-box,.notification-list-item')]
              .some(node => node.innerText.toLowerCase().includes(value.toLowerCase())))
        }))()`);
        const notification = await cdp.evaluate(`[...document.querySelectorAll('.notification-list-item')].map(node=>node.innerText).join(' ').slice(0,16000)`);
        if (notification && fixtureEnv) {
          const candidateRuntime = fixtureEnv.JOSH_ROOM_ACCEPTANCE_RCC && JSON.parse(fixtureEnv.JOSH_ROOM_ACCEPTANCE_RCC);
          const sanitizeCommand = candidateRuntime?.command || python;
          const sanitizePrefix = candidateRuntime ? ["--no-build", "env", "exec", "--artifact", candidateRuntime.artifact,
            "--permissive-local", "--inherit-streams", "--", "python"] : [];
          const sanitized = spawnSync(sanitizeCommand, [...sanitizePrefix, path.join(__dirname, "windows_recovery_provider.py"), "--sanitize-ui"], {
            env: fixtureEnv, input: notification, encoding: "utf8", timeout: 10000, maxBuffer: 65536,
          });
          receipt.native_notification = sanitized.status === 0 ? JSON.parse(sanitized.stdout) : { diagnostic: "sanitizer unavailable" };
        }
      } catch { receipt.native_widget = { diagnostic: "unavailable" }; }
    }
  }
  finally {
    cdp?.socket.close();
    if (editor?.pid && editor.exitCode === null) {
      try { run("taskkill", ["/pid", String(editor.pid), "/T", "/F"]); }
      catch { failure ||= new Error("Owned editor cleanup failed"); }
    }
    if (provider) {
      await fsp.writeFile(path.join(base, "stop-provider"), "stop");
      await until(() => provider.exitCode !== null, "owned native credential cleanup", 30000);
      for (const name of ["provider-ready.json", "provider-failed.json", "provider-cli-failed.json", "explicit-input-denials.json",
        "provider-progress.json", "ui-driver-ready.json", "ui-command-started.json", "ui-command-finished.json",
        "ui-command-failed.json", "ui-process-state.json", "ui-controller-summary.json", "ui-editor-focus.json", "ui-diff.json", "custody-cleanup.json", "preservation.json"]) {
        if (fs.existsSync(path.join(base, name))) {
          const value = JSON.parse(await fsp.readFile(path.join(base, name)));
          await fsp.writeFile(path.join(evidence, name), JSON.stringify(value, null, 2));
        }
      }
      const cleanup = path.join(base, "custody-cleanup.json");
      receipt.cleanup = fs.existsSync(cleanup) && JSON.parse(await fsp.readFile(cleanup)).state === "PASS" ? "PASS" : "FAIL";
      if (provider.exitCode !== 0) { failure ||= new Error("Provider acceptance failed"); receipt.provider_state = "FAIL"; }
      if (fs.existsSync(path.join(base, "provider-errors.log"))) {
        // Only sanitized exception type/boundary; never copy raw provider or editor logs.
        receipt.provider_error_log_bytes = (await fsp.stat(path.join(base, "provider-errors.log"))).size;
      }
    }
    const handoffProbes = path.join(base, "existing-handoff-probes.jsonl");
    if (fs.existsSync(handoffProbes)) {
      const records = (await fsp.readFile(handoffProbes, "utf8")).trim().split("\n").filter(Boolean).map(line => JSON.parse(line));
      await fsp.writeFile(path.join(evidence, "existing-handoff-evidence.json"), JSON.stringify({ records }, null, 2));
    }
    receipt.state = failure ? "FAIL" : "PASS";
    await fsp.writeFile(path.join(evidence, "installed-windows-recovery.json"), JSON.stringify(receipt, null, 2));
    await fsp.rm(base, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
  if (failure) throw failure;
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1; });
module.exports = { digest, pins };
