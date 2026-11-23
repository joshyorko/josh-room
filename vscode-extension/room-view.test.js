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
    cwd,
    env: harness.runtime.env,
    detached: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  assert.deepEqual(runtimeCall.args, [
    "view", "serve", "--provider", "room", "--workspace", cwd,
    "--port", "0", "--require-token",
  ]);
  assert.equal(runtimeCall.receipt.startsWith("/tmp/josh-room-view-"), true);
  assert.equal(harness.opened[0], "http://127.0.0.1:49152/?token=synthetic-token");
  assert.equal(session.url, harness.opened[0]);
  assert.equal(session.active, true);
  await session.dispose();
  assert.equal(session.active, false);
  assert.equal(fs.existsSync(runtimeCall.receipt), false);
});

test("launcher blocks non-Linux and remote VS Code before spawning", async () => {
  const local = createHarness();
  await assert.rejects(launchRoomView({ ...local, cwd: "/workspace", platform: "win32" }), /only on local Linux/);
  assert.equal(local.calls.length, 0);

  const remote = createHarness();
  remote.vscode.env.remoteName = "ssh-remote";
  await assert.rejects(launchRoomView({ ...remote, cwd: "/workspace", platform: "linux" }), /remote VS Code/);
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
