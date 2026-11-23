"use strict";

const assert = require("node:assert/strict");
const { execPath } = require("node:process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const test = require("node:test");

const { launchRoomView, validateRoomViewInfo } = require("./room-view");

function createHarness({ autoStart = true, pid = 1_987_654_321, preamble = "", onSpawn, info = {
  ok: true,
  command: "view-serve",
  host: "127.0.0.1",
  port: 49152,
  url: "http://127.0.0.1:49152/?token=synthetic-token",
  read_only: true,
} } = {}) {
  const calls = [];
  const child = new EventEmitter();
  child.pid = pid;
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kill = (signal) => {
    child.lastSignal = signal;
    if (!child.closed) {
      child.closed = true;
      child.emit("close", null, signal);
    }
  };
  const opened = [];
  const runtime = {
    command: "/managed/rcc",
    env: { PATH: "/managed/bin" },
    args(args, receipt) {
      calls.push({ args, receipt });
      return ["--no-build", "env", "exec", "--receipt-file", receipt, "--inherit-streams", "--json", "--", "python", "-m", "josh_room", ...args, "--json"];
    },
  };
  const vscode = {
    UIKind: { Web: 2 },
    workspace: { isTrusted: true },
    Uri: { parse: (value) => ({ value }) },
    env: { uiKind: 1, openExternal: async (uri) => { opened.push(uri.value); return true; } },
  };
  const spawn = (command, args, options) => {
    calls.push({ command, argv: args, options, child });
    onSpawn?.({ child, args, options });
    if (autoStart) setImmediate(() => child.stdout.emit("data", Buffer.from(`${preamble}${JSON.stringify(info)}\n`)));
    return child;
  };
  const kill = (pidValue, signal) => {
    assert.equal(pidValue, -child.pid);
    child.kill(signal);
  };
  return { calls, child, opened, runtime, spawn, kill, vscode };
}

test("launcher starts the exact selected Room workspace through the managed runtime and opens verified loopback URL", async () => {
  const harness = createHarness();
  const cwd = "/workspace/selected-room";
  const session = await launchRoomView({ ...harness, cwd, platform: "linux" });
  const launch = harness.calls.find((call) => call.command === "/managed/rcc");
  const runtimeCall = harness.calls.find((call) => call.args);
  assert.equal(launch.command, "/managed/rcc");
  assert.deepEqual(launch.options, {
    cwd: require("node:path").join(__dirname, "runtime", "controller"),
    env: { ...harness.runtime.env, PYTHONSAFEPATH: "1" },
    detached: true,
    stdio: ["pipe", "pipe", "pipe"],
  });
  assert.deepEqual(runtimeCall.args, [
    "view", "serve", "--provider", "room", "--workspace", cwd,
    "--port", "0", "--require-token", "--stop-on-stdin-eof",
  ]);
  assert.equal(runtimeCall.receipt.startsWith(require("node:path").join(require("node:os").tmpdir(), "josh-room-view-")), true);
  assert.equal(harness.opened[0], "http://127.0.0.1:49152/?token=synthetic-token");
  assert.equal(session.url, harness.opened[0]);
  assert.equal(session.active, true);
  await session.dispose();
  assert.equal(session.active, false);
  assert.equal(fs.existsSync(runtimeCall.receipt), false);
});

test("launcher blocks non-Linux and remote hosts without private forwarding before spawning", async () => {
  const local = createHarness();
  await assert.rejects(launchRoomView({ ...local, cwd: "/workspace", platform: "win32" }), /Linux workspace extension host/);
  assert.equal(local.calls.length, 0);

  const remote = createHarness();
  remote.vscode.env.remoteName = "ssh-remote";
  await assert.rejects(launchRoomView({ ...remote, cwd: "/workspace", platform: "linux" }), /remote VS Code host/);
  assert.equal(remote.calls.length, 0);

  const untrusted = createHarness();
  untrusted.vscode.workspace.isTrusted = false;
  await assert.rejects(launchRoomView({ ...untrusted, cwd: "/workspace", platform: "linux" }), /trusted VS Code workspace/);
  assert.equal(untrusted.calls.length, 0);

  const web = createHarness();
  web.vscode.env.uiKind = web.vscode.UIKind.Web;
  await assert.rejects(launchRoomView({ ...web, cwd: "/workspace", platform: "linux" }), /VS Code for the web/);
  assert.equal(web.calls.length, 0);
});

test("startup URL validation rejects non-loopback, mismatched port, and non-HTTP results", () => {
  const valid = { ok: true, command: "view-serve", host: "127.0.0.1", port: 1234, url: "http://127.0.0.1:1234/?token=x", read_only: true };
  assert.equal(validateRoomViewInfo(valid), valid.url);
  assert.throws(() => validateRoomViewInfo({ ...valid, url: "http://localhost:1234/" }), /loopback/);
  assert.throws(() => validateRoomViewInfo({ ...valid, url: "http://127.0.0.1:1235/" }), /loopback/);
  assert.throws(() => validateRoomViewInfo({ ...valid, url: "https://127.0.0.1:1234/" }), /loopback/);
  assert.throws(() => validateRoomViewInfo({ ...valid, url: "http://127.0.0.1:1234/" }), /loopback/);
});

test("invalid startup data stops the child and does not expose raw output", async () => {
  const harness = createHarness({ info: { ok: false, error: "synthetic private detail" } });
  await assert.rejects(launchRoomView({ ...harness, cwd: "/workspace", platform: "linux" }), { code: "ROOM_VIEW_CONTROLLER_FAILED" });
  assert.equal(harness.child.lastSignal, "SIGKILL");
});

test("bounded RCC preambles and blank lines are ignored before the view startup JSON", async () => {
  const harness = createHarness({ preamble: "RCC status: starting\n\n" });
  const session = await launchRoomView({ ...harness, cwd: "/workspace", platform: "linux" });
  await session.dispose();
  assert.equal(harness.opened.length, 1);
});

test("stderr diagnostics are bounded, sanitized, and never expose URL tokens", async () => {
  const harness = createHarness({ onSpawn: ({ child }) => setImmediate(() => {
    child.stderr.emit("data", Buffer.from("failed at http://127.0.0.1:41873/?token=secret-value\n"));
  }) });
  const diagnostics = [];
  const session = await launchRoomView({
    ...harness,
    cwd: "/workspace",
    platform: "linux",
    sanitizeDiagnostic: (line) => line,
    onDiagnostic: (line) => diagnostics.push(line),
  });
  assert.match(diagnostics[0], /token=\[redacted\]/);
  assert.doesNotMatch(diagnostics[0], /secret-value/);
  await session.dispose();
});

test("prestartup receipt must be bounded, regular, and match the selected controller", async () => {
  const harness = createHarness({ autoStart: false, onSpawn: ({ args, child }) => {
    const index = args.indexOf("--receipt-file");
    fs.writeFileSync(args[index + 1], JSON.stringify({ artifactDigest: "sha256:wrong" }));
    setImmediate(() => child.emit("close", 1));
  } });
  harness.runtime.controllerArtifact = "sha256:expected";
  await assert.rejects(launchRoomView({ ...harness, cwd: "/workspace", platform: "linux" }), { code: "ROOM_VIEW_RECEIPT_MISMATCH" });
  assert.equal(fs.existsSync(harness.calls.find((call) => call.args).receipt), false);
});

test("asynchronous spawn error without a PID cleans up without waiting for a process close", async () => {
  const harness = createHarness({ autoStart: false, pid: undefined, onSpawn: ({ child }) => {
    setImmediate(() => child.emit("error", new Error("private spawn detail")));
  } });
  const started = Date.now();
  await assert.rejects(launchRoomView({ ...harness, cwd: "/workspace", platform: "linux" }), { code: "ROOM_VIEW_SPAWN_FAILED" });
  assert.ok(Date.now() - started < 250);
  assert.equal(fs.existsSync(harness.calls.find((call) => call.args).receipt), false);
});

test("cancellation during startup terminates the process and removes its receipt directory", async () => {
  const harness = createHarness({ autoStart: false });
  let listener;
  const cancellationToken = {
    isCancellationRequested: false,
    onCancellationRequested(callback) {
      listener = callback;
      return { dispose() {} };
    },
  };
  const pending = launchRoomView({ ...harness, cwd: "/workspace", platform: "linux", cancellationToken });
  while (!listener) await new Promise((resolve) => setImmediate(resolve));
  cancellationToken.isCancellationRequested = true;
  listener();
  await assert.rejects(pending, { name: "AbortError", code: "ABORT_ERR" });
  assert.equal(harness.child.lastSignal, "SIGKILL");
  assert.equal(fs.existsSync(harness.calls.find((call) => call.args).receipt), false);
});

test("startup timeout terminates the child", async () => {
  const harness = createHarness({ autoStart: false });
  await assert.rejects(launchRoomView({ ...harness, cwd: "/workspace", platform: "linux", startupTimeoutMs: 5 }), { code: "ROOM_VIEW_STARTUP_TIMEOUT" });
  assert.equal(harness.child.lastSignal, "SIGKILL");
});

test("real Linux child starts, opens, and is reaped by session disposal", { skip: process.platform !== "linux" }, async t => {
  const path = require("node:path");
  const cwd = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "room-view-real-child-"));
  t.after(() => fs.rmSync(cwd, { recursive: true, force: true }));
  const pidFile = path.join(cwd, "child.pid");
  const { spawn, kill, ...harness } = createHarness();
  harness.runtime.command = execPath;
  harness.runtime.args = () => ["-e", `require("node:fs").writeFileSync(${JSON.stringify(pidFile)}, String(process.pid)); process.stdout.write(${JSON.stringify(`${JSON.stringify({
    ok: true,
    command: "view-serve",
    host: "127.0.0.1",
    port: 41873,
    url: "http://127.0.0.1:41873/?token=synthetic-token",
    read_only: true,
  })}\n`)}); setInterval(() => {}, 1000);`];
  const session = await launchRoomView({ ...harness, cwd, platform: "linux" });
  t.after(() => session.dispose());
  const pid = Number(fs.readFileSync(pidFile, "utf8"));
  assert.equal(session.url, "http://127.0.0.1:41873/?token=synthetic-token");
  process.kill(pid, 0);
  await session.dispose();
  assert.equal(session.active, false);
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
});

test("review regression: cancellation settles while browser handoff is pending", async () => {
  const h = createHarness();
  const entered = Promise.withResolvers(), browser = Promise.withResolvers();
  let cancel;
  const token = { onCancellationRequested(f) { cancel = f; return { dispose() {} }; } };
  h.vscode.env.openExternal = () => { entered.resolve(); return browser.promise; };
  const result = launchRoomView({ ...h, cwd: process.cwd(), cancellationToken: token })
    .then(() => "opened", error => error.code);
  await entered.promise;
  token.isCancellationRequested = true;
  cancel();
  const observed = await Promise.race([result, new Promise(r => setTimeout(() => r("pending"), 150))]);
  browser.resolve(true);
  await result;
  assert.equal(observed, "ABORT_ERR");
});

test("review regression: a closed child cannot open the browser", async () => {
  const { spawn, kill, ...h } = createHarness();
  const record = { ok: true, command: "view-serve", host: "127.0.0.1",
    port: 41234, read_only: true, url: "http://127.0.0.1:41234/?token=fixture" };
  h.runtime.command = process.execPath;
  h.runtime.args = () => ["-e", "process.stdout.write(process.argv[1])", JSON.stringify(record)];
  await assert.rejects(launchRoomView({ ...h, cwd: process.cwd() }), { code: "ROOM_VIEW_EXITED_EARLY" });
  assert.equal(h.opened.length, 0);
});

test("review regression: provider overlap refusal stays actionable without exposing arbitrary errors", async () => {
  const overlap = createHarness({ info: { ok: false, error: "Room View cannot observe the managed runtime directory" } });
  await assert.rejects(launchRoomView({ ...overlap, cwd: process.cwd() }), error =>
    error.code === "ROOM_VIEW_RUNTIME_OVERLAP" && /outside that directory/.test(error.message));
  const unknown = createHarness({ info: { ok: false, error: "synthetic private diagnostic" } });
  await assert.rejects(launchRoomView({ ...unknown, cwd: process.cwd() }), error =>
    error.code === "ROOM_VIEW_CONTROLLER_FAILED" && !error.message.includes("private"));
});

test("launcher sends the exact selected Room binding to the controller", async () => {
  const h = createHarness();
  const session = await launchRoomView({ ...h, cwd: process.cwd(), roomBinding: { room_id: "demo", dimension_id: "alpha", encryption_domain_id: "domain", composition_namespace: null, device: "1", inode: "2" } });
  const args = h.calls.find(call => call.args).args;
  assert.equal(args.at(-2), "--expected-workspace-binding");
  assert.equal(JSON.parse(args.at(-1)).encryption_domain_id, "domain");
  await session.dispose();
});

for (const remoteName of ["dev-container", "attached-container", "ssh-remote"]) {
  test(`Linux ${remoteName} forwards a tokenized viewer privately to the desktop browser`, async () => {
    const h = createHarness();
    h.vscode.env.remoteName = remoteName;
    h.vscode.workspace.getConfiguration = () => ({ get: () => "localhost" });
    let forwarded = 0;
    h.vscode.env.asExternalUri = async uri => {
      assert.equal(uri.value, "http://127.0.0.1:49152/?token=synthetic-token");
      forwarded++;
      return { value: "http://127.0.0.1:53123/?token=synthetic-token", toString: () => "http://127.0.0.1:53123/?token=synthetic-token" };
    };
    const session = await launchRoomView({ ...h, cwd: process.cwd() });
    assert.equal(forwarded, 1);
    assert.equal(h.opened[0], "http://127.0.0.1:53123/?token=synthetic-token");
    assert.equal(session.url, h.opened[0]);
    await session.dispose();
  });
}

test("remote viewer refuses public localPortHost before spawning and unsafe mapped URLs before opening", async () => {
  for (const url of ["https://public.invalid/?token=synthetic-token", "http://127.0.0.1:53123/", "http://0.0.0.0:53123/?token=synthetic-token"]) {
    const h = createHarness();
    h.vscode.env.remoteName = "dev-container";
    h.vscode.workspace.getConfiguration = () => ({ get: () => "localhost" });
    h.vscode.env.asExternalUri = async () => ({ toString: () => url });
    await assert.rejects(launchRoomView({ ...h, cwd: process.cwd() }), { code: "ROOM_VIEW_FORWARD_FAILED" });
    assert.equal(h.opened.length, 0);
    assert.equal(h.child.closed, true);
  }
  const h = createHarness();
  h.vscode.env.remoteName = "dev-container";
  h.vscode.workspace.getConfiguration = () => ({ get: () => "allInterfaces" });
  h.vscode.env.asExternalUri = async () => { throw new Error("must not forward"); };
  await assert.rejects(launchRoomView({ ...h, cwd: process.cwd() }), /localPortHost/);
  assert.equal(h.calls.length, 0);
});

test("cancelling a pending private forward settles and cleans the Linux process", async () => {
  const h = createHarness();
  h.vscode.env.remoteName = "ssh-remote";
  h.vscode.workspace.getConfiguration = () => ({ get: () => "localhost" });
  const entered = Promise.withResolvers();
  const forwarding = Promise.withResolvers();
  h.vscode.env.asExternalUri = () => { entered.resolve(); return forwarding.promise; };
  let cancel;
  const token = { onCancellationRequested(cb) { cancel = cb; return { dispose() {} }; } };
  const result = launchRoomView({ ...h, cwd: process.cwd(), cancellationToken: token });
  await entered.promise;
  token.isCancellationRequested = true; cancel();
  await assert.rejects(result, { code: "ABORT_ERR" });
  assert.equal(h.child.closed, true);
  assert.equal(h.opened.length, 0);
  forwarding.resolve({ toString: () => "http://localhost:53123/?token=synthetic-token" });
});

test("private forwarding setting is checked again after runtime startup", async () => {
  let setting = "localhost";
  const h = createHarness({ onSpawn: () => { setting = "allInterfaces"; } });
  h.vscode.env.remoteName = "dev-container";
  h.vscode.workspace.getConfiguration = () => ({ get: () => setting });
  let forwards = 0;
  h.vscode.env.asExternalUri = async () => { forwards++; };
  await assert.rejects(launchRoomView({ ...h, cwd: process.cwd() }), { code: "ROOM_VIEW_FORWARD_PRIVATE_REQUIRED" });
  assert.equal(forwards, 0);
  assert.equal(h.child.closed, true);
});

test("real Python starts outside a Room containing shadowing modules and stops when its owner dies", { timeout: 15000, skip: process.platform !== "linux" }, async t => {
  const path = require("node:path");
  const os = require("node:os");
  const cp = require("node:child_process");
  const net = require("node:net");
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "room-view-owner-"));
  const root = path.join(base, "room"); fs.mkdirSync(root);
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  for (const name of ["secrets.py", "types.py", "typing.py", "logging.py", "dataclasses.py"])
    fs.writeFileSync(path.join(root, name), "raise RuntimeError('Room code must not execute')\n");
  fs.writeFileSync(path.join(root, "example.txt"), "synthetic");
  const python = process.env.JOSH_ROOM_TEST_PYTHON || path.join(__dirname, "..", ".venv", "bin", "python");
  if (!fs.existsSync(python)) return t.skip("Python project runtime unavailable; set JOSH_ROOM_TEST_PYTHON");
  const pidFile = path.join(base, "pid");
  const urlFile = path.join(base, "url");
  const code = `
    const fs = require('node:fs');
    const cp = require('node:child_process');
    const {launchRoomView} = require(${JSON.stringify(require.resolve("./room-view"))});
    launchRoomView({ cwd:${JSON.stringify(root)},
      runtime:{command:${JSON.stringify(python)},env:{PATH:process.env.PATH,PYTHONPATH:${JSON.stringify(path.join(__dirname, "runtime", "controller"))}},args:a=>['-m','josh_room',...a,'--json']},
      spawn:(...args)=>{const child=cp.spawn(...args);fs.writeFileSync(${JSON.stringify(pidFile)},String(child.pid));return child;},
      vscode:{workspace:{isTrusted:true},Uri:{parse:x=>x},env:{openExternal:async()=>true}}
    }).then(s=>fs.writeFileSync(${JSON.stringify(urlFile)},s.url)).catch(e=>{console.error(e);process.exit(1)});
  `;
  const owner = cp.spawn(execPath, ["-e", code], { stdio: ["ignore", "ignore", "pipe"] });
  let diagnostic = ""; owner.stderr.on("data", chunk => { diagnostic += chunk; });
  t.after(() => {
    try { owner.kill("SIGKILL"); } catch {}
    if (fs.existsSync(pidFile)) { try { process.kill(-Number(fs.readFileSync(pidFile, "utf8")), "SIGKILL"); } catch {} }
  });
  const deadline = Date.now() + 10000;
  while (!fs.existsSync(urlFile) && owner.exitCode === null && Date.now() < deadline)
    await new Promise(resolve => setTimeout(resolve, 25));
  assert.equal(fs.existsSync(urlFile), true, diagnostic);
  const url = new URL(fs.readFileSync(urlFile, "utf8"));
  const http = require("node:http");
  const state = await new Promise((resolve, reject) => {
    http.get(new URL('/state.json' + url.search, url), response => {
      let body=''; response.on('data', chunk=>body+=chunk); response.on('end',()=>resolve({status:response.statusCode,body}));
    }).on('error', reject);
  });
  assert.equal(state.status, 200);
  assert.match(state.body, /example.txt/);
  owner.kill("SIGKILL");
  const stopped = () => new Promise(resolve => {
    const socket=net.connect({host:'127.0.0.1',port:Number(url.port)});
    socket.once('connect',()=>{socket.destroy();resolve(false)});
    socket.once('error',error=>{socket.destroy();resolve(error.code==='ECONNREFUSED')});
  });
  let closed = false;
  while (!(closed = await stopped()) && Date.now() < deadline) await new Promise(resolve=>setTimeout(resolve,25));
  assert.equal(closed, true, "Viewer listener survived abrupt owner death");
});

test("leader close kills descendants before receipt IO and disposal awaits receipt validation", async t => {
  const promises = require("node:fs/promises");
  const h = createHarness();
  const session = await launchRoomView({ ...h, cwd: "/workspace" });
  const receipt = h.calls.find(call => call.receipt).receipt;
  const original = promises.open;
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  promises.open = async (...args) => { if (args[0] === receipt) await gate; return original(...args); };
  t.after(() => { promises.open = original; release(); });
  h.child.closed = true;
  h.child.emit("close", 0);
  assert.equal(h.child.lastSignal, "SIGKILL");
  let finished = false;
  const disposal = session.dispose().then(() => { finished = true; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finished, false);
  assert.equal(fs.existsSync(require("node:path").dirname(receipt)), true);
  release();
  await disposal;
  assert.equal(fs.existsSync(require("node:path").dirname(receipt)), false);
});
