"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const Module = require("node:module");
const { isListenerStopped } = require("./installed_room_view_probe");

test("a surviving 403 listener is not stopped", async t => {
  const server = http.createServer((_request, response) => { response.writeHead(403); response.end(); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  assert.equal(await isListenerStopped(`http://127.0.0.1:${server.address().port}/`), false);
});

test("installed driver rejects a failed Explore even after receiving a URL", async t => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "room-view-driver-"));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const server = http.createServer((_request, response) => { response.writeHead(403); response.end(); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}/?token=synthetic`;
  const helperPath = path.join(base, "room-view.js");
  fs.writeFileSync(helperPath, "exports.launchRoomView = async options => { await options.vscode.env.openExternal({toString: () => options.cwd}); return {active:true}; };\n");
  const helper = require(helperPath);
  const vscode = {
    extensions: { getExtension: () => ({ extensionPath: base, activate: async () => {} }) },
    commands: { executeCommand: async name => {
      if (name !== "joshRoom.explore3D") return;
      try { await helper.launchRoomView({ cwd: url, runtime: { command: "fixture", controllerArtifact: "synthetic" },
        vscode: { workspace: {}, Uri: {}, env: {} } }); }
      catch { return "failed"; }
    } },
  };
  const previous = process.env.JOSH_ROOM_ACCEPTANCE_ROOT;
  process.env.JOSH_ROOM_ACCEPTANCE_ROOT = base;
  t.after(() => { if (previous === undefined) delete process.env.JOSH_ROOM_ACCEPTANCE_ROOT; else process.env.JOSH_ROOM_ACCEPTANCE_ROOT = previous; });
  const loader = Module._load;
  Module._load = function(request, ...args) { return request === "vscode" ? vscode : loader.call(this, request, ...args); };
  const script = require.resolve("./installed_room_view_acceptance");
  delete require.cache[script];
  let driver;
  try { driver = require(script); } finally { Module._load = loader; }
  t.after(() => { delete require.cache[script]; delete require.cache[helperPath]; });
  await assert.rejects(driver.run(), /did not complete a verified desktop handoff/);
  assert.equal(await isListenerStopped(url), false);
});
