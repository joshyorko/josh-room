"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { prepareWindowsHandoff } = require("./windows-handoff");

test("native handoff helper uses the OS executable and metadata-only environment", async () => {
  let calls = 0;
  await prepareWindowsHandoff("C:\\owned\\handoff", { systemRoot: "C:\\Windows", execute(executable, args, options, done) {
    calls++;
    assert.equal(executable, "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe");
    assert.equal(options.env.JOSH_ROOM_HANDOFF_ROOT, "C:\\owned\\handoff");
    assert.ok(args.includes("-NoProfile") && args.includes("-NonInteractive"));
    assert.equal(options.timeout, 10000);
    done(null);
  } });
  assert.equal(calls, 1);
});

test("native ACL failure and missing OS authority never fall back", async () => {
  await assert.rejects(prepareWindowsHandoff("C:\\owned", { systemRoot: "", execute() { assert.fail(); } }), /unavailable/);
  await assert.rejects(prepareWindowsHandoff("C:\\owned", { systemRoot: "C:\\Windows", execute(_exe, _args, _options, done) {
    done(new Error("synthetic failure"));
  } }), /preparation failed/);
});
