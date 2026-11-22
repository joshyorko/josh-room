"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { applyInstalledRuntime } = require("./installed_recovery_runtime");
const { candidateRuntime } = require("./installed_recovery_runtime");
const { runCandidatePhase } = require("./installed_recovery_runtime");

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

test("RCC timeout retains exact phase and safe progress without argv, paths or raw output", () => {
  const records = [];
  let clock = 0;
  assert.throws(() => runCandidatePhase("acquire-archive", "private/rcc.exe", ["private/archive"], {
    record: event => records.push(event), now: () => clock++ * 180000,
    invoke: (_command, _args, options) => {
      assert.equal(options.timeout, 180000);
      return { status: null, error: { code: "ETIMEDOUT" }, stdout: "private identity content",
        stderr: "private/account/path Progress: 14/15 extracting archive" };
    },
  }), /acquire-archive timed out/);
  assert.equal(records[0].state, "RUNNING");
  assert.equal(records[1].state, "FAIL");
  assert.equal(records[1].timed_out, true);
  assert.deepEqual(records[1].progress_steps, [[14, 15]]);
  assert.ok(records[1].progress_categories.includes("extract"));
  assert.doesNotMatch(JSON.stringify(records), /private|identity content|account/);
});

test("RCC probe has a separate short deadline and returns actual output only on success", () => {
  const records = [];
  const output = runCandidatePhase("interpreter-probe", "rcc.exe", [], {
    record: event => records.push(event), invoke: (_command, _args, options) => {
      assert.equal(options.timeout, 60000);
      return { status: 0, stdout: ' {"origin":"actual"} ', stderr: "" };
    },
  });
  assert.equal(output, '{"origin":"actual"}');
  assert.equal(records[1].state, "PASS");
  assert.throws(() => runCandidatePhase("solve", "rcc.exe", [], { record: () => {} }), /Invalid RCC/);
});
