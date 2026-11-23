"use strict";

const assert = require("node:assert/strict");
const { execPath } = require("node:process");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const test = require("node:test");

const { launchRoomView, validateRoomViewInfo } = require("./room-view");

function createHarness({ autoStart = true, info = {
  ok: true,
  command: "view-serve",
  host: "127.0.0.1",
  port: 49152,
  url: "http://127.0.0.1:49152/?token=synthetic-token",
  read_only: true,
} } = {}) {
  const calls = [];
  const child = new EventEmitter();
  child.pid = 1_987_654_321;
  child.stdout = new EventEmitter();
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
    workspace: { isTrusted: true },
    Uri: { parse: (value) => ({ value }) },
    env: { openExternal: async (uri) => { opened.push(uri.value); return true; } },
  };
  const spawn = (command, args, options) => {
    calls.push({ command, argv: args, options, child });
    if (autoStart) setImmediate(() => child.stdout.emit("data", Buffer.from(`${JSON.stringify(info)}\n`)));
    return child;
  };
  return { calls, child, opened, runtime, spawn, vscode };
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
    stdio: ["ignore", "pipe", "ignore"],
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
  await assert.rejects(launchRoomView({ ...harness, cwd: "/workspace", platform: "linux" }), { code: "ROOM_VIEW_STARTUP_INVALID" });
  assert.equal(harness.child.lastSignal, "SIGKILL");
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

test("real Linux child starts, opens, and is reaped by session disposal", { skip: process.platform !== "linux" }, async () => {
  const harness = createHarness();
  harness.runtime.command = execPath;
  harness.runtime.args = () => ["-e", `process.stdout.write(${JSON.stringify(`${JSON.stringify({
    ok: true,
    command: "view-serve",
    host: "127.0.0.1",
    port: 41873,
    url: "http://127.0.0.1:41873/?token=synthetic-token",
    read_only: true,
  })}\n`)}); setInterval(() => {}, 1000);`];
  const session = await launchRoomView({ ...harness, cwd: "/workspace", platform: "linux" });
  assert.equal(session.active, true);
  await session.dispose();
  assert.equal(session.active, false);
});
