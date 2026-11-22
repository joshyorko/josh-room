"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { applyInstalledRuntime } = require("./installed_recovery_runtime");
const { candidateRuntime } = require("./installed_recovery_runtime");

test("Windows runtime override reaches canonical activated instances, not another extension", () => {
  const received = [];
  const entry = filename => ({ filename, exports: { __test__: { setRuntimeForTests: value => received.push([filename, value]) } } });
  const cache = { before: entry("C:\\fixture\\extensions\\josh-room\\extension.js"),
    active: entry("c:\\fixture\\extensions\\josh-room\\extension.js"),
    unrelated: entry("C:\\other\\extension.js") };
  const runtime = { mode: "local-build-fallback" };
  assert.equal(applyInstalledRuntime("C:\\fixture\\extensions\\josh-room", runtime,
    { cache, realpath: value => value, platform: "win32" }).length, 2);
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

test("explicit unpublished RCC candidate executes no-build with artifact-bound receipts", () => {
  const candidate = { command: pathAbsolute("rcc.exe"), subject_sha: "a".repeat(40), artifact: "sha256:" + "b".repeat(64) };
  const env = { JOSH_ROOM_ACCEPTANCE_ROOT: pathAbsolute("fixture"), JOSH_ROOM_JAT_ROOT: pathAbsolute("empty-jat") };
  const runtime = candidateRuntime(candidate, env);
  const args = runtime.args(["recovery", "tree"], "owned-receipt.json");
  assert.equal(runtime.command, candidate.command);
  assert.equal(runtime.mode, "managed");
  assert.equal(runtime.controllerArtifact, candidate.artifact);
  assert.deepEqual(args.slice(0, 9), ["--no-build", "env", "exec", "--artifact", candidate.artifact,
    "--permissive-local", "--receipt-file", "owned-receipt.json", "--inherit-streams"]);
  assert.ok(args.includes("python"));
  assert.equal(args.includes("run"), false);
  assert.equal(runtime.jatRoot, env.JOSH_ROOM_JAT_ROOT);
});

test("candidate mode never repairs malformed subject, artifact or command using source Python", () => {
  const valid = { command: pathAbsolute("rcc.exe"), subject_sha: "a".repeat(40), artifact: "sha256:" + "b".repeat(64) };
  for (const invalid of [{ command: "python" }, { subject_sha: "latest" }, { artifact: "sha256:bad" }]) {
    assert.throws(() => candidateRuntime({ ...valid, ...invalid }, {}), /Invalid explicit RCC/);
  }
});

function pathAbsolute(name) { return require("node:path").resolve(name); }
