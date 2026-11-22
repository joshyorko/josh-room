"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { applyInstalledRuntime } = require("./installed_recovery_runtime");
const { candidateRuntime } = require("./installed_recovery_runtime");
const { runCandidatePhase } = require("./installed_recovery_runtime");
const { candidateProcessOptions } = require("./installed_recovery_runtime");
const { candidateExecPrefix, verifyCandidateReceipt } = require("./installed_recovery_runtime");

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

test("candidate acquisition replaces inherited HOME and MAMBA scope and uses installed controller cwd", () => {
  const path = require("node:path");
  const base = path.resolve("owned");
  const home = path.join(base, "managed", "rcc");
  const controller = path.join(base, "extension", "runtime", "controller");
  const parent = { HOME: "foreign", MAMBA_ROOT_PREFIX: "foreign-mamba", ROBOCORP_HOME: "foreign-rcc",
    RCC_HOLOTREE_MODE: "shared", TEMP: path.join(base, "provider-runtime"), TMP: path.join(base, "provider-runtime") };
  const actual = candidateProcessOptions(base, home, controller, parent);
  assert.equal(actual.cwd, controller);
  assert.equal(actual.env.HOME, path.dirname(home));
  assert.equal(actual.env.MAMBA_ROOT_PREFIX, path.join(home, "mamba"));
  assert.equal(actual.env.ROBOCORP_HOME, home);
  assert.equal(actual.env.RCC_HOLOTREE_MODE, "private");
  assert.equal(actual.env.TEMP, parent.TEMP);
  assert.equal(actual.env.TMP, parent.TMP);
  assert.equal(parent.HOME, "foreign");
});

test("candidate acquisition rejects paths outside its task root", () => {
  const path = require("node:path");
  const base = path.resolve("owned");
  for (const [home, controller] of [[path.resolve("foreign"), path.join(base, "controller")],
    [path.join(base, "rcc"), path.resolve("foreign-controller")]]) {
    assert.throws(() => candidateProcessOptions(base, home, controller, {}), /owned fixture/);
  }
});

test("inherited RCC execution always supplies its required receipt before the child separator", () => {
  const artifact = "sha256:" + "b".repeat(64);
  const args = candidateExecPrefix(artifact, "owned-receipt.json");
  assert.equal(args[args.indexOf("--receipt-file") + 1], "owned-receipt.json");
  assert.ok(args.indexOf("--receipt-file") < args.indexOf("--"));
  assert.ok(args.includes("--no-build"));
  assert.throws(() => candidateExecPrefix(artifact), /require an artifact and receipt/);
  const runtime = candidateRuntime({ command: pathAbsolute("rcc"), subject_sha: "a".repeat(40), artifact },
    { JOSH_ROOM_ACCEPTANCE_ROOT: pathAbsolute("fixture") });
  assert.ok(runtime.args(["status"]).includes("--receipt-file"));
});

test("probe receipt admission rejects missing, malformed, wrong-artifact and nonzero exit results", () => {
  const artifact = "sha256:" + "b".repeat(64);
  const read = value => () => JSON.stringify(value);
  assert.deepEqual(verifyCandidateReceipt("owned", artifact, read({ artifactDigest: artifact, exitCode: 0 })),
    { artifact, exit_code: 0, verified: true });
  for (const value of [{ artifactDigest: "sha256:" + "c".repeat(64), exitCode: 0 },
    { artifactDigest: artifact, exitCode: 1 }, { artifactDigest: artifact, exitCode: "0" }]) {
    assert.throws(() => verifyCandidateReceipt("owned", artifact, read(value)), /artifact or exit mismatch/);
  }
  assert.throws(() => verifyCandidateReceipt("owned", artifact, () => { throw new Error("private missing path"); }), /receipt missing or invalid/);
  assert.throws(() => verifyCandidateReceipt("owned", artifact, () => "private malformed content"), /receipt missing or invalid/);
});

test("probe failure retains only allowlisted error categories", () => {
  const records = [];
  assert.throws(() => runCandidatePhase("interpreter-probe", "rcc", [], {
    record: event => records.push(event), invoke: () => ({ status: 1, stdout: "",
      stderr: "private ambient value: --receipt-file is required with --inherit-streams" }),
  }), /interpreter-probe failed/);
  assert.deepEqual(records[1].failure_categories, ["missing-inherited-streams-receipt"]);
  assert.doesNotMatch(JSON.stringify(records), /ambient|private/);
});
