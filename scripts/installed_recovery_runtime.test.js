"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { applyInstalledRuntime } = require("./installed_recovery_runtime");

test("Windows runtime override reaches canonical activated instances, not another extension", () => {
  const received = [];
  const entry = filename => ({ filename, exports: { __test__: { setRuntimeForTests: value => received.push([filename, value]) } } });
  const cache = { before: entry("C:\\fixture\\extensions\\josh-room\\extension.js"),
    active: entry("c:\\fixture\\extensions\\josh-room\\extension.js"),
    unrelated: entry("C:\\other\\extension.js") };
  const runtime = { mode: "local-build-fallback" };
  assert.equal(applyInstalledRuntime("C:\\fixture\\extensions\\josh-room", runtime,
    { cache, realpath: value => value, platform: "win32" }), 2);
  assert.equal(received.length, 2);
  assert.ok(received.every(([, value]) => value === runtime));
});

test("missing activated hook fails closed instead of silently importing RCC", () => {
  assert.throws(() => applyInstalledRuntime("/owned", {}, { cache: {}, realpath: value => value }), /not found/);
});

test("POSIX case-distinct installed paths never share the override", () => {
  const cache = { other: { filename: "/Owned/extension.js", exports: { __test__: { setRuntimeForTests: () => assert.fail() } } } };
  assert.throws(() => applyInstalledRuntime("/owned", {}, { cache, realpath: value => value, platform: "linux" }), /not found/);
});
