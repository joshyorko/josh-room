const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const zlib = require("node:zlib");

const {
  ensureControllerRuntime,
  ensureJatRuntime,
  ensureManagedRcc,
  localFallbackReason,
  readManifest,
  resolvePlatform,
  runtimeEnvironment,
  selectControllerArtifact,
  selectJatArtifact,
} = require("./runtime");

const HAULER_VERSION_CHECK = "import os, shutil, subprocess, sys; executable = shutil.which('hauler'); prefix = os.environ.get('CONDA_PREFIX'); prefix_root = os.path.realpath(prefix) if prefix else ''; resolved = os.path.realpath(executable) if executable else ''; python_resolved = os.path.realpath(sys.executable); inside = bool(prefix_root and resolved.startswith(prefix_root + os.sep)); python_inside = bool(prefix_root and python_resolved.startswith(prefix_root + os.sep)); sys.exit(127 if not (inside and python_inside) else subprocess.run([resolved, 'version'], check=False).returncode)";

function tarMember(name, body, type = "0") {
  const content = Buffer.from(body);
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, "utf8");
  header.write("0000600\0", 100, 8, "ascii");
  header.write("0000000\0", 108, 8, "ascii");
  header.write("0000000\0", 116, 8, "ascii");
  header.write(`${content.length.toString(8).padStart(11, "0")}\0`, 124, 12, "ascii");
  header.write("00000000000\0", 136, 12, "ascii");
  header.write("        ", 148, 8, "ascii");
  header.write(type, 156, 1, "ascii");
  header.write("ustar\0", 257, 6, "ascii");
  header.write("00", 263, 2, "ascii");
  const checksum = [...header].reduce((sum, value) => sum + value, 0);
  header.write(`${checksum.toString(8).padStart(6, "0")}\0 `, 148, 8, "ascii");
  const padding = Buffer.alloc((512 - (content.length % 512)) % 512);
  return Buffer.concat([header, content, padding]);
}

function digest(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function manifestFor(binary, overrides = {}) {
  return {
    schema_version: 1,
    extension_version: "0.1.1",
    rcc: {
      version: "v18.19.2",
      platforms: {
        "linux-x64": {
          asset: "rcc-linux64",
          url: "https://github.com/joshyorko/rcc/releases/download/v18.19.2/rcc-linux64",
          sha256: digest(binary),
        },
      },
    },
    ...overrides,
  };
}

function context(root) {
  return { globalStorageUri: { fsPath: root } };
}

function cancellationSource() {
  const listeners = new Set();
  return {
    token: {
      isCancellationRequested: false,
      onCancellationRequested(listener) {
        listeners.add(listener);
        return { dispose: () => listeners.delete(listener) };
      },
    },
    cancel() {
      this.token.isCancellationRequested = true;
      for (const listener of listeners) listener();
    },
    listeners,
  };
}

function controllerFixture() {
  const archive = Buffer.from("synthetic-controller-archive");
  const artifact = "sha256:" + "f".repeat(64);
  const pin = {
    asset: "controller.rcca",
    url: "https://github.com/joshyorko/josh-room/releases/download/test/controller.rcca",
    sha256: digest(archive), size: archive.length,
  };
  return { archive, artifact, manifest: manifestFor(Buffer.from("rcc"), {
    controller: { environment_artifact: { digest: artifact, archive: pin } },
  }) };
}

const cancelledError = { name: "AbortError", code: "ABORT_ERR" };

test("pre-cancelled runtime preparation does not create storage or invoke seams", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-cancel-before-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  source.cancel();
  const runtime = require("./runtime");
  const target = context(path.join(root, "absent"));
  const { manifest } = controllerFixture();
  const rcc = { executable: process.execPath, version: "v18.19.2" };
  const options = { cancellationToken: source.token, runJson: async () => ({}), download: async () => {} };
  for (const prepare of [
    () => runtime.ensureManagedRcc(target, manifest, options),
    () => runtime.ensureControllerRuntime(target, manifest, rcc, options),
    () => runtime.ensureJatRuntime(target, manifest, rcc, options),
    () => runtime.ensureJatSource(target, {}, options),
    () => runtime.prepareLocalController(target, rcc, "robot.yaml", options),
    () => runtime.verifyLocalFallback(target, rcc, "robot.yaml", {}, options),
    () => runtime.buildLocalJatArtifact(target, rcc, "robot.yaml", options),
  ]) await assert.rejects(prepare(), cancelledError);
  assert.equal(fs.existsSync(target.globalStorageUri.fsPath), false);
  assert.equal(source.listeners.size, 0);
});

test("controller download cancellation cleans staging and a retry verifies before import", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-controller-cancel-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  const { manifest, archive, artifact } = controllerFixture();
  const rcc = { executable: process.execPath, version: "v18.19.2" };
  let imports = 0;
  const runJson = async () => { imports += 1; return { artifactDigest: artifact, verification: { valid: true } }; };
  await assert.rejects(ensureControllerRuntime(context(root), manifest, rcc, {
    cancellationToken: source.token,
    download: async (_url, destination, options) => {
      assert.equal(options?.cancellationToken, source.token);
      fs.writeFileSync(destination, archive);
      source.cancel();
    },
    runJson,
  }), cancelledError);
  const archiveRoot = path.join(root, "runtime", "controller-artifact");
  assert.deepEqual(fs.readdirSync(archiveRoot), []);
  assert.equal(imports, 0);
  const events = [];
  const options = {
    download: async (_url, destination) => {
      assert.match(events.at(-1).message, /Downloading.*controller/i);
      fs.writeFileSync(destination, archive);
    },
    onProgress: (event) => events.push(event), runJson,
  };
  await ensureControllerRuntime(context(root), manifest, rcc, options);
  assert.ok(events.some((event) => /Verifying.*controller/i.test(event.message)));
  events.length = 0;
  await ensureControllerRuntime(context(root), manifest, rcc, options);
  assert.ok(events.some((event) => /Verifying.*cached.*controller/i.test(event.message)));
  assert.ok(events.some((event) => event.phase === "reuse"));
  assert.equal(events.some((event) => /Downloading/i.test(event.message)), false);
});

test("HTTPS download aborts a redirected active response, removes partials and permits retry", { timeout: 5000 }, async (t) => {
  const http = require("node:http");
  const https = require("node:https");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-http-cancel-"));
  const source = cancellationSource();
  const binary = Buffer.from("synthetic-rcc-download");
  let complete = false;
  let requestClosed = false;
  const server = http.createServer((request, response) => {
    if (request.url !== "/redirected") {
      response.writeHead(302, { location: "https://example.invalid/redirected" });
      response.end();
      return;
    }
    response.writeHead(200);
    response.write(binary.subarray(0, 5));
    if (complete) response.end(binary.subarray(5));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => { server.closeAllConnections(); server.close(); fs.rmSync(root, { recursive: true, force: true }); });
  t.mock.method(https, "get", (_url, options, callback) => {
    const request = http.get(`http://127.0.0.1:${server.address().port}${_url.pathname}`, options, (response) => {
      callback(response);
      if (!complete && response.statusCode === 200) response.once("data", () => source.cancel());
    });
    if (_url.pathname === "/redirected") request.once("close", () => { requestClosed = true; });
    return request;
  });
  await assert.rejects(ensureManagedRcc(context(root), manifestFor(binary), {
    cancellationToken: source.token, verifyVersion: async () => {},
  }), cancelledError);
  assert.equal(requestClosed, true);
  assert.equal(source.listeners.size, 0);
  assert.deepEqual(fs.readdirSync(path.join(root, "runtime", "rcc", "v18.19.2", "linux-x64")), []);
  complete = true;
  const result = await ensureManagedRcc(context(root), manifestFor(binary), { verifyVersion: async () => {} });
  assert.deepEqual(fs.readFileSync(result.executable), binary);
});

test("RCC version cancellation does not promote the binary or report readiness", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-version-cancel-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  const binary = Buffer.from("synthetic-rcc");
  const phases = [];
  await assert.rejects(ensureManagedRcc(context(root), manifestFor(binary), {
    cancellationToken: source.token,
    download: async (_url, destination) => fs.writeFileSync(destination, binary),
    verifyVersion: async (_executable, _version, options) => {
      assert.equal(options?.cancellationToken, source.token);
      source.cancel();
    },
    onProgress: (event) => phases.push(event.message),
  }), cancelledError);
  assert.deepEqual(fs.readdirSync(path.join(root, "runtime", "rcc", "v18.19.2", "linux-x64")), []);
  assert.equal(phases.includes("Managed RCC ready"), false);
});

test("local RCC cancellation waits for a real subprocess and its child to exit", { skip: process.platform === "win32", timeout: 5000 }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-process-cancel-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const executable = path.join(root, "rcc");
  fs.writeFileSync(executable, `#!${process.execPath}
const { spawn } = require('node:child_process');
const child = spawn(process.execPath, ['-e', "process.on('SIGTERM', () => setTimeout(() => process.exit(0), 150)); console.log('ready'); setTimeout(() => process.exit(0), 1500);"], { stdio: ['ignore', 'pipe', 'pipe'] });
process.on('SIGTERM', () => {});
child.stdout.once('data', () => process.stderr.write('ready ' + process.pid + ' ' + child.pid + '\\n'));
child.on('close', () => { console.log('{"vars":[]}'); process.exit(0); });
`, { mode: 0o700 });
  const source = cancellationSource();
  let pids;
  const start = Date.now();
  await assert.rejects(require("./runtime").prepareLocalController(context(root), { executable }, path.join(root, "robot.yaml"), {
    cancellationToken: source.token,
    onOutput: (stream, chunk) => {
      const match = /ready (\d+) (\d+)/.exec(chunk);
      if (stream === "stderr" && match) { pids = match.slice(1).map(Number); source.cancel(); }
    },
  }), cancelledError);
  assert.ok(pids);
  for (const pid of pids) assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
  assert.ok(Date.now() - start < 1400, "cancellation should stop the child before its natural exit");
  assert.equal(source.listeners.size, 0);
});

test("RCC stdout JSON remains private while stderr progress is forwarded", { skip: process.platform === "win32" }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-output-private-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const executable = path.join(root, "rcc");
  fs.writeFileSync(executable, `#!${process.execPath}\nconsole.error('Preparing environment'); console.log(JSON.stringify([{key:'SYNTHETIC_PRIVATE_VALUE', value:'fixture-only'}]));\n`, { mode: 0o700 });
  const output = [];
  const result = await require("./runtime").prepareLocalController(context(root), { executable }, path.join(root, "robot.yaml"), {
    onOutput: (stream, chunk) => output.push({ stream, chunk }),
  });
  assert.equal(result[0].value, "fixture-only");
  assert.ok(output.some(({ chunk }) => chunk.includes("Preparing environment")));
  assert.equal(output.some(({ stream }) => stream === "stdout"), false);
});

test("warm fallback cancellation propagates instead of authorizing a rebuild", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-warm-cancel-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  const runtime = require("./runtime");
  const expected = { schema_version: 1, mode: "local-build-fallback" };
  await runtime.writeLocalFallbackRecord(context(root), expected);
  await assert.rejects(runtime.verifyLocalFallback(context(root), { executable: process.execPath }, "robot.yaml", expected, {
    cancellationToken: source.token,
    runJson: async (_executable, _args, options) => {
      assert.equal(options?.cancellationToken, source.token);
      source.cancel();
      throw Object.assign(new Error("cancelled"), cancelledError);
    },
  }), cancelledError);
});

test("cancelling a cached artifact probe cannot fall through to archive import", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-probe-cancel-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  const { manifest, archive } = controllerFixture();
  const archiveRoot = path.join(root, "runtime", "controller-artifact");
  fs.mkdirSync(archiveRoot, { recursive: true });
  fs.writeFileSync(path.join(archiveRoot, "controller.rcca"), archive);
  const calls = [];
  await assert.rejects(ensureControllerRuntime(context(root), manifest, { executable: process.execPath, version: "v18.19.2" }, {
    cancellationToken: source.token,
    runJson: async (_executable, args) => {
      calls.push(args);
      source.cancel();
      throw new Error("artifact is not local and no provider was supplied");
    },
  }), cancelledError);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].includes("--artifact"), true);
});

test("controller does not claim cached materialization reuse when RCC must import it", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-reuse-progress-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const { manifest, archive, artifact } = controllerFixture();
  const archiveRoot = path.join(root, "runtime", "controller-artifact");
  fs.mkdirSync(archiveRoot, { recursive: true });
  fs.writeFileSync(path.join(archiveRoot, "controller.rcca"), archive);
  const events = [];
  await ensureControllerRuntime(context(root), manifest, { executable: process.execPath, version: "v18.19.2" }, {
    onProgress: (event) => events.push(event),
    runJson: async (_executable, args) => {
      if (args.includes("--artifact")) throw new Error("artifact is not local and no provider was supplied");
      return { artifactDigest: artifact, verification: { valid: true } };
    },
  });
  assert.equal(events.some((event) => event.phase === "reuse"), false);
  assert.ok(events.some((event) => event.phase === "import"));
});

test("local JAT publish cancellation releases its lock and skips verification on retryable cancellation", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-publish-cancel-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  const runtime = require("./runtime");
  const artifact = "sha256:" + "a".repeat(64);
  const calls = [];
  await assert.rejects(runtime.buildLocalJatArtifact(context(root), { executable: process.execPath }, "robot.yaml", {
    cancellationToken: source.token,
    runJson: async (_executable, args, options) => {
      assert.equal(options.cancellationToken, source.token);
      calls.push(args);
      source.cancel();
      return { artifactDigest: artifact };
    },
  }), cancelledError);
  assert.equal(calls.length, 1);
  assert.equal(fs.existsSync(path.join(root, "runtime", "local-jat-build.lock")), false);
  const result = await runtime.buildLocalJatArtifact(context(root), { executable: process.execPath }, "robot.yaml", {
    runJson: async () => ({ artifactDigest: artifact, exitCode: 0 }),
  });
  assert.equal(result.artifact, artifact);
});

test("cancelling a JAT lock waiter leaves the other builder's lock intact", { timeout: 2000 }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-lock-cancel-"));
  const lock = path.join(root, "runtime", "local-jat-build.lock");
  fs.mkdirSync(lock, { recursive: true });
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  const timer = setTimeout(() => source.cancel(), 20);
  t.after(() => clearTimeout(timer));
  await assert.rejects(require("./runtime").buildLocalJatArtifact(context(root), { executable: process.execPath }, "robot.yaml", {
    cancellationToken: source.token,
    runJson: async () => { throw new Error("must not start a second builder"); },
  }), cancelledError);
  assert.equal(fs.existsSync(lock), true);
});

test("RCC version cancellation kills a real child that ignores SIGTERM before settling", { skip: process.platform === "win32", timeout: 5000 }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-version-child-cancel-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = cancellationSource();
  let pid;
  const binary = Buffer.from(`#!${process.execPath}\nprocess.on('SIGTERM', () => {}); console.error('ready ' + process.pid); setTimeout(() => console.log('v18.19.2'), 3000);\n`);
  await assert.rejects(ensureManagedRcc(context(root), manifestFor(binary), {
    cancellationToken: source.token,
    download: async (_url, destination) => fs.writeFileSync(destination, binary),
    onOutput: (_stream, chunk) => {
      const match = /ready (\d+)/.exec(chunk);
      if (match) { pid = Number(match[1]); source.cancel(); }
    },
  }), cancelledError);
  assert.ok(pid);
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" });
  assert.deepEqual(fs.readdirSync(path.join(root, "runtime", "rcc", "v18.19.2", "linux-x64")), []);
  assert.equal(source.listeners.size, 0);
});

test("invalid RCC stdout never appears in surfaced JSON errors", { skip: process.platform === "win32" }, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-json-error-private-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const executable = path.join(root, "rcc");
  fs.writeFileSync(executable, `#!${process.execPath}\nconsole.log('SYNTHETIC_PRIVATE_VALUE invalid JSON');\n`, { mode: 0o700 });
  await assert.rejects(require("./runtime").prepareLocalController(context(root), { executable }, path.join(root, "robot.yaml")), (error) => {
    assert.match(error.message, /invalid JSON/);
    assert.equal(error.message.includes("SYNTHETI"), false);
    return true;
  });
});

test("JAT cancellation covers archive, source, acquire and Hauler verification without leaving staging", async (t) => {
  for (const stage of ["archive", "source", "acquire", "hauler"]) await t.test(stage, async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-seam-cancel-"));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    const source = cancellationSource();
    const gitSha = "a".repeat(40);
    const archive = Buffer.from("synthetic-jat-artifact");
    const artifact = "sha256:" + "b".repeat(64);
    const sourceArchive = zlib.gzipSync(Buffer.concat([tarMember("source/robot.yaml", "tasks: {}\n"), Buffer.alloc(1024)]));
    const manifest = manifestFor(Buffer.from("rcc"), { jat: {
      git_sha: gitSha,
      source_archive: { asset: "source.tar.gz", url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + gitSha, sha256: digest(sourceArchive) },
      environment_artifact: { digest: artifact, archive: {
        asset: "jat.rcca", url: "https://github.com/joshyorko/josh-all-the-things/releases/download/test/jat.rcca", sha256: digest(archive), size: archive.length,
      } },
    } });
    const calls = [];
    await assert.rejects(ensureJatRuntime(context(root), manifest, { executable: process.execPath, version: "v18.19.2" }, {
      cancellationToken: source.token,
      download: async (url, destination, options) => {
        assert.equal(options.cancellationToken, source.token);
        const current = url.endsWith("jat.rcca") ? "archive" : "source";
        calls.push(current);
        fs.writeFileSync(destination, current === "archive" ? archive : sourceArchive);
        if (current === stage) source.cancel();
      },
      runJson: async (_executable, args, options) => {
        assert.equal(options.cancellationToken, source.token);
        const current = args[1] === "acquire" ? "acquire" : "hauler";
        calls.push(current);
        if (current === stage) source.cancel();
        return { artifactDigest: artifact, verification: { valid: true }, exitCode: 0 };
      },
    }), cancelledError);
    assert.deepEqual(calls, ["archive", "source", "acquire", "hauler"].slice(0, ["archive", "source", "acquire", "hauler"].indexOf(stage) + 1));
    for (const directory of ["jat-artifact", "jat"]) {
      const target = path.join(root, "runtime", directory);
      if (fs.existsSync(target)) assert.equal(fs.readdirSync(target).some((entry) => entry.startsWith(".")), false);
    }
  });
});

test("resolvePlatform accepts only the first supported Linux mapping", () => {
  assert.equal(resolvePlatform("linux", "x64"), "linux-x64");
  assert.equal(resolvePlatform("win32", "x64"), "win32-x64");
  assert.throws(() => resolvePlatform("linux", "arm64"), /not supported/);
  assert.throws(() => resolvePlatform("darwin", "x64"), /does not support macOS yet/);
});

test("controller artifacts are selected by runtime platform", () => {
  const linux = { digest: "sha256:" + "a".repeat(64) };
  const windows = { digest: "sha256:" + "b".repeat(64) };
  const controller = {
    environment_artifact: linux,
    environment_artifacts: { "linux-x64": linux, "win32-x64": windows },
  };
  assert.equal(selectControllerArtifact(controller, "linux-x64"), linux);
  assert.equal(selectControllerArtifact(controller, "win32-x64"), windows);
  assert.throws(
    () => selectControllerArtifact({ environment_artifact: linux }, "win32-x64"),
    /controller environment artifact pin for win32-x64/,
  );
});

test("local fallback eligibility is limited to compatibility and unpublished controller artifacts", () => {
  assert.equal(localFallbackReason({ fallbackReason: "environment-compatibility" }), "environment-compatibility");
  assert.equal(localFallbackReason({ fallbackReason: "controller-artifact-unpublished" }), "controller-artifact-unpublished");
  assert.equal(localFallbackReason({ fallbackReason: "checksum-mismatch" }), undefined);
  assert.equal(localFallbackReason(new Error("credentials unavailable")), undefined);
});

test("local fallback warm reuse requires the complete scoped identity", async () => {
  const runtime = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-fallback-marker-test-"));
  const runtimeContext = context(runtime);
  const expected = {
    mode: "local-build-fallback",
    extension_version: "0.1.5",
    rcc_version: "v18.19.2",
    platform: "linux-x64",
    jat_source_sha: "a".repeat(40),
    jat_artifact_digest: "sha256:" + "b".repeat(64),
    controller_source_version: "c".repeat(64),
    controller_artifact_digest: "unpublished",
  };
  const api = require("./runtime");
  await api.writeLocalFallbackRecord(runtimeContext, expected);
  assert.equal(api.localFallbackRecordMatches(api.readLocalFallbackRecord(runtimeContext), expected), true);
  assert.equal(api.localFallbackRecordMatches(api.readLocalFallbackRecord(runtimeContext), { ...expected, extension_version: "0.1.10" }), false);
});

test("local fallback controller preparation runs managed RCC before readiness resolves", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-local-prewarm-test-"));
  const calls = [];
  const phases = [];
  const runtime = require("./runtime");
  await assert.rejects(
    runtime.prepareLocalController(
      context(root),
      { executable: "/private/managed/rcc", version: "v18.19.2" },
      "/private/controller/robot.yaml",
      {
        runJson: async (_executable, args) => {
          calls.push(args);
          throw new Error("local controller preparation failed");
        },
        onProgress: (event) => phases.push(event.message),
      },
    ),
    /local controller preparation failed/,
  );
  assert.deepEqual(calls, [["ht", "vars", "-r", "/private/controller/robot.yaml", "--json"]]);
  assert.deepEqual(phases, ["Building controller environment locally"]);
  assert.equal(fs.existsSync(runtime.localFallbackRecordPath(context(root))), false);
});

test("RCC JSON framing accepts ht vars arrays with streamed output around them", () => {
  const runtime = require("./runtime");
  const variables = [{ key: "PYTHON_EXE", value: "/managed/python" }, { key: "CONDA_PREFIX", value: "/managed" }];
  assert.deepEqual(runtime.parseJsonOutput(JSON.stringify(variables)), variables);
  assert.deepEqual(
    runtime.parseJsonOutput(`RCC progress line\n${JSON.stringify(variables, null, 2)}\nRCC finished\n`),
    variables,
  );
});

test("warm local fallback proof uses no-build ht vars and the exact private RCC home", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-local-warm-test-"));
  const runtime = require("./runtime");
  const expected = { schema_version: 1, mode: "local-build-fallback", extension_version: "0.1.10", platform: "linux-x64" };
  await runtime.writeLocalFallbackRecord(context(root), expected);
  const calls = [];
  assert.equal(await runtime.verifyLocalFallback(context(root), { executable: "/private/managed/rcc", version: "v18.19.2" }, "/private/controller/robot.yaml", expected, {
    runJson: async (_executable, args, options) => {
      calls.push({ args, options });
      return { vars: [] };
    },
  }), true);
  assert.deepEqual(calls[0].args, ["--no-build", "ht", "vars", "--robot", "/private/controller/robot.yaml", "--json"]);
  assert.equal(calls[0].options.env.ROBOCORP_HOME, path.join(root, "robocorp"));
});

test("local JAT fallback publishes once and verifies Hauler through the local artifact", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-local-jat-artifact-test-"));
  const calls = [];
  const artifact = "sha256:" + "d".repeat(64);
  const result = await require("./runtime").buildLocalJatArtifact(
    context(root),
    { executable: "/private/managed/rcc", version: "v18.19.2" },
    "/private/jat/robot.yaml",
    {
      runJson: async (_executable, args, options) => {
        calls.push({ args, options });
        if (args[1] === "publish") return { artifactDigest: artifact, specificationDigest: "sha256:" + "e".repeat(64), legacyBlueprintKey: "blueprint" };
        return { artifactDigest: artifact, exitCode: 0, verification: { valid: true } };
      },
    },
  );
  assert.equal(result.artifact, artifact);
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[0].args, ["env", "publish", "--robot", "/private/jat/robot.yaml", "--provider", "local", "--json"]);
  assert.equal(calls[1].args.includes("--no-build"), true);
  assert.equal(calls[1].args.includes("--artifact"), true);
  assert.equal(calls[1].args.includes("hauler"), true);
  assert.equal(calls[1].options.env.RCC_HOLOTREE_MODE, "private");
});

test("readManifest rejects an RCC pin without an exact digest", () => {
  assert.throws(
    () => readManifest({ schema_version: 1, extension_version: "0.1.1", rcc: { version: "v18.19.2", platforms: { "linux-x64": {} } } }),
    /platform pin|sha256|platform/i,
  );
});

test("readManifest rejects an RCC pin outside the official release host", () => {
  const binary = Buffer.from("managed-rcc-binary");
  const manifest = manifestFor(binary);
  manifest.rcc.platforms["linux-x64"].url = "https://example.invalid/rcc";
  assert.throws(() => readManifest(manifest), /official RCC release host/);
});

test("ensureManagedRcc verifies the downloaded binary before atomic promotion", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-runtime-test-"));
  const binary = Buffer.from("managed-rcc-binary");
  const manifest = manifestFor(binary);
  let downloaded = 0;
  const result = await ensureManagedRcc(context(root), manifest, {
    platform: "linux-x64",
    download: async (_url, destination) => {
      downloaded += 1;
      fs.writeFileSync(destination, binary);
    },
    verifyVersion: async (executable, version) => {
      assert.equal(fs.readFileSync(executable).toString(), binary.toString());
      assert.equal(version, "v18.19.2");
    },
  });

  assert.equal(downloaded, 1);
  assert.equal(fs.readFileSync(result.executable).toString(), binary.toString());
  assert.equal(result.executable.startsWith(root), true);
  assert.equal(fs.readdirSync(path.dirname(result.executable)).some((name) => name.includes("tmp")), false);
});

test("ensureManagedRcc reports ordered acquisition, verification, and cached reuse phases", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-runtime-phases-test-"));
  const binary = Buffer.from("managed-rcc-binary");
  const manifest = manifestFor(binary);
  const phases = [];
  const options = {
    platform: "linux-x64",
    onProgress: (event) => phases.push(event),
    download: async (_url, destination) => fs.writeFileSync(destination, binary),
    verifyVersion: async () => {},
  };
  await ensureManagedRcc(context(root), manifest, options);
  await ensureManagedRcc(context(root), manifest, options);
  assert.deepEqual(phases.map((event) => event.message), [
    "Resolving managed RCC",
    "Downloading RCC",
    "Verifying RCC SHA256",
    "Managed RCC ready",
    "Resolving managed RCC",
    "Reusing cached verified RCC",
  ]);
});

test("ensureManagedRcc refuses a corrupt cached binary without replacing it", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-runtime-cache-test-"));
  const binary = Buffer.from("managed-rcc-binary");
  const manifest = manifestFor(binary);
  const expected = path.join(root, "runtime", "rcc", "v18.19.2", "linux-x64", "rcc");
  fs.mkdirSync(path.dirname(expected), { recursive: true });
  fs.writeFileSync(expected, "corrupt");
  let downloaded = 0;

  await assert.rejects(
    ensureManagedRcc(context(root), manifest, {
      platform: "linux-x64",
      download: async () => { downloaded += 1; },
      verifyVersion: async () => {},
    }),
    /checksum|digest|corrupt/i,
  );
  assert.equal(downloaded, 0);
  assert.equal(fs.readFileSync(expected, "utf8"), "corrupt");
});

test("ensureManagedRcc reports the exact missing Windows runtime dependency", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-runtime-win32-pin-test-"));
  await assert.rejects(
    require("./runtime").ensureManagedRcc(context(root), manifestFor(Buffer.from("rcc")), {
      platform: "win32-x64",
      download: async () => { throw new Error("download must not run without a Windows pin"); },
    }),
    /Windows RCC binary and matching Windows JAT environment artifact must be published and pinned first/,
  );
});

test("runtimeEnvironment keeps RCC and Room state under extension global storage", () => {
  const root = "/private/vscode/global-storage/josh-room";
  const environment = runtimeEnvironment(context(root), {
    rccExecutable: `${root}/runtime/rcc/v18.19.2/linux-x64/rcc`,
    jatArtifact: "sha256:" + "a".repeat(64),
    jatSourceSha: "b".repeat(40),
  }, "/workspaces/example");

  assert.equal(environment.RCC_HOLOTREE_MODE, "private");
  assert.equal(environment.ROBOCORP_HOME, `${root}/robocorp`);
  assert.equal(environment.JOSH_ROOM_RCC_EXE, `${root}/runtime/rcc/v18.19.2/linux-x64/rcc`);
  assert.equal(environment.JOSH_ROOM_WORKSPACE_ROOT, "/workspaces/example");
  assert.equal(environment.JOSH_ROOM_JAT_ARTIFACT, "sha256:" + "a".repeat(64));
  assert.equal(environment.JOSH_ROOM_JAT_SHA, "b".repeat(40));
});

test("runtimeEnvironment gives RCC a space-safe home for VS Code paths with spaces", () => {
  const root = "/private/vscode/Code - Insiders/User/globalStorage/josh-room";
  const environment = runtimeEnvironment(context(root), {}, "/workspaces/example");

  assert.equal(/\s/.test(environment.ROBOCORP_HOME), false);
  assert.notEqual(environment.ROBOCORP_HOME, path.join(root, "robocorp"));
  assert.equal(environment.JOSH_ROOM_RCC_HOME, environment.ROBOCORP_HOME);
  assert.equal(environment.JOSH_ROOM_INSTANCE, path.join(root, "state", "josh-room"));
  assert.equal(environment.JOSH_ROOM_CONFIG_DIR, path.join(root, "config"));
});

test("selectJatArtifact chooses the platform pin and preserves the Linux legacy fallback", () => {
  const legacy = { digest: "sha256:" + "a".repeat(64) };
  const linux = { digest: "sha256:" + "b".repeat(64) };
  const windows = { digest: "sha256:" + "c".repeat(64) };
  const jat = {
    environment_artifact: legacy,
    environment_artifacts: { "linux-x64": linux, "win32-x64": windows },
  };

  assert.deepEqual(selectJatArtifact(jat, "linux-x64"), linux);
  assert.deepEqual(selectJatArtifact(jat, "win32-x64"), windows);
  assert.deepEqual(selectJatArtifact({ environment_artifact: legacy }, "linux-x64"), legacy);
});

test("selectJatArtifact fails closed when Windows has no platform artifact", () => {
  assert.throws(
    () => selectJatArtifact({ environment_artifact: { digest: "sha256:" + "a".repeat(64) } }, "win32-x64"),
    /missing a JAT environment artifact pin for win32-x64/,
  );
});

test("ensureJatRuntime validates and acquires the selected Windows artifact", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-windows-jat-runtime-test-"));
  const rccBinary = Buffer.from("managed-rcc-binary");
  const legacyArchive = Buffer.from("legacy-rcca-archive");
  const windowsArchive = Buffer.from("windows-rcca-archive");
  const legacyArtifact = "sha256:" + "a".repeat(64);
  const windowsArtifact = "sha256:" + "b".repeat(64);
  const manifest = manifestFor(rccBinary, {
    jat: {
      git_sha: "d".repeat(40),
      source_archive: {
        asset: "josh-all-the-things.tar.gz",
        url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + "d".repeat(40),
        sha256: "e".repeat(64),
      },
      environment_artifact: {
        digest: legacyArtifact,
        archive: {
          asset: "jat-runtime-linux-amd64.rcca",
          url: "https://github.com/joshyorko/josh-all-the-things/releases/download/v0.1.1/jat-runtime-linux-amd64.rcca",
          sha256: digest(legacyArchive),
          size: legacyArchive.length,
        },
      },
      environment_artifacts: {
        "win32-x64": {
          digest: windowsArtifact,
          archive: {
            asset: "jat-runtime-windows-amd64.rcca",
            url: "https://github.com/joshyorko/josh-all-the-things/releases/download/v0.1.1/jat-runtime-windows-amd64.rcca",
            sha256: digest(windowsArchive),
            size: windowsArchive.length,
          },
        },
      },
    },
  });
  const calls = [];
  const downloads = [];
  const result = await ensureJatRuntime(
    context(root),
    manifest,
    { executable: `${root}/runtime/rcc`, version: "v18.19.2" },
    {
      platform: "win32-x64",
      download: async (url, destination) => {
        downloads.push(url);
        fs.writeFileSync(destination, windowsArchive);
      },
      ensureSource: async () => path.join(root, "jat-source"),
      runJson: async (executable, args, options) => {
        calls.push({ executable, args, options });
        if (args[1] === "acquire") return { artifactDigest: windowsArtifact, verification: { valid: true } };
        if (args.at(-2) === "hauler" && args.at(-1) === "version") {
          return { artifactDigest: windowsArtifact, exitCode: 0 };
        }
        return { artifactDigest: windowsArtifact, exitCode: 0 };
      },
    },
  );

  assert.equal(result.artifact, windowsArtifact);
  assert.deepEqual(downloads, ["https://github.com/joshyorko/josh-all-the-things/releases/download/v0.1.1/jat-runtime-windows-amd64.rcca"]);
  assert.equal(calls[0].args.some((arg) => arg.endsWith("jat-runtime-windows-amd64.rcca")), true);
  assert.equal(calls[1].args.includes(windowsArtifact), true);
});

test("ensureJatRuntime acquires the pinned archive and proves Hauler through the artifact", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-runtime-test-"));
  const rccBinary = Buffer.from("managed-rcc-binary");
  const jatArchive = Buffer.from("rcca-archive");
  const artifact = "sha256:" + "c".repeat(64);
  const manifest = manifestFor(rccBinary, {
    jat: {
      git_sha: "d".repeat(40),
      source_archive: {
        asset: "josh-all-the-things.tar.gz",
        url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + "d".repeat(40),
        sha256: "e".repeat(64),
      },
      environment_artifact: {
        digest: artifact,
        archive: {
          asset: "jat-runtime-linux-amd64.rcca",
          url: "https://github.com/joshyorko/josh-all-the-things/releases/download/v0.1.1/jat-runtime-linux-amd64.rcca",
          sha256: digest(jatArchive),
          size: jatArchive.length,
        },
      },
    },
  });
  const calls = [];
  let downloads = 0;
  const result = await require("./runtime").ensureJatRuntime(
    context(root),
    manifest,
    { executable: `${root}/runtime/rcc`, version: "v18.19.2" },
    {
      platform: "linux-x64",
      download: async (_url, destination) => {
        downloads += 1;
        fs.writeFileSync(destination, jatArchive);
      },
      ensureSource: async () => path.join(root, "jat-source"),
      runJson: async (executable, args, options) => {
        calls.push({ executable, args, options });
        if (args[1] === "acquire") return { artifactDigest: artifact, verification: { valid: true } };
        if (args.at(-2) === "hauler" && args.at(-1) === "version") {
          return { artifactDigest: artifact, exitCode: 127 };
        }
        return { artifactDigest: artifact, exitCode: 0 };
      },
    },
  );

  assert.equal(downloads, 1);
  assert.equal(result.artifact, artifact);
  assert.equal(result.sourceSha, "d".repeat(40));
  assert.deepEqual(calls.map((call) => call.args.slice(0, 3)), [
    ["env", "acquire", "--archive"],
    ["--no-build", "env", "exec"],
  ]);
  assert.equal(calls[0].args.includes("--permissive-local"), true);
  assert.equal(calls[1].args.includes("--permissive-local"), true);
  assert.equal(calls[1].args.includes("--no-build"), true);
  assert.equal(calls[1].args.includes("--inherit-streams"), true);
  assert.equal(calls[1].args.includes("--receipt-file"), true);
  assert.ok(calls[1].args[calls[1].args.indexOf("--receipt-file") + 1].startsWith(root));
  assert.deepEqual(calls[1].args.slice(-3), ["python", "-c", HAULER_VERSION_CHECK]);
  assert.equal(calls[0].options.env.ROBOCORP_HOME, path.join(root, "robocorp"));
  assert.equal(calls[0].options.env.RCC_HOLOTREE_MODE, "private");
});

test("cached JAT archive reuses local artifact digest without reimporting 6596 archive objects", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-cached-jat-runtime-"));
  const archive = Buffer.from("cached-jat-rcca");
  const artifact = "sha256:" + "a".repeat(64);
  const asset = "jat-runtime-linux-amd64.rcca";
  const artifactRoot = path.join(root, "runtime", "jat-artifact");
  fs.mkdirSync(artifactRoot, { recursive: true });
  fs.writeFileSync(path.join(artifactRoot, asset), archive);
  const manifest = manifestFor(Buffer.from("rcc"), {
    jat: {
      git_sha: "b".repeat(40),
      source_archive: { asset: "source.tar.gz", url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + "b".repeat(40), sha256: "c".repeat(64) },
      environment_artifact: {
        digest: artifact,
        archive: { asset, url: "https://github.com/joshyorko/josh-all-the-things/releases/download/test/" + asset, sha256: digest(archive), size: archive.length },
      },
    },
  });
  const calls = [];
  await ensureJatRuntime(context(root), manifest, { executable: "/managed/rcc", version: "v18.19.3" }, {
    ensureSource: async () => path.join(root, "jat-source"),
    runJson: async (_executable, args) => {
      calls.push(args);
      return args[1] === "acquire"
        ? { artifactDigest: artifact, verification: { valid: true }, cacheHit: "local-materialization" }
        : { artifactDigest: artifact, exitCode: 0 };
    },
  });
  assert.equal(calls[0].includes("--artifact"), true);
  assert.equal(calls[0].includes("--archive"), false);
  assert.equal(calls[0].includes(artifact), true);
});

test("cached JAT archive imports only when the local RCC artifact is genuinely absent", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-cached-jat-recovery-"));
  const archive = Buffer.from("cached-jat-rcca");
  const artifact = "sha256:" + "e".repeat(64);
  const asset = "jat-runtime-linux-amd64.rcca";
  const artifactRoot = path.join(root, "runtime", "jat-artifact");
  fs.mkdirSync(artifactRoot, { recursive: true });
  fs.writeFileSync(path.join(artifactRoot, asset), archive);
  const manifest = manifestFor(Buffer.from("rcc"), {
    jat: {
      git_sha: "f".repeat(40),
      source_archive: { asset: "source.tar.gz", url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + "f".repeat(40), sha256: "a".repeat(64) },
      environment_artifact: {
        digest: artifact,
        archive: { asset, url: "https://github.com/joshyorko/josh-all-the-things/releases/download/test/" + asset, sha256: digest(archive), size: archive.length },
      },
    },
  });
  const calls = [];
  await ensureJatRuntime(context(root), manifest, { executable: "/managed/rcc", version: "v18.19.3" }, {
    ensureSource: async () => path.join(root, "jat-source"),
    runJson: async (_executable, args) => {
      calls.push(args);
      if (calls.length === 1) throw new Error("artifact is not local and no provider was supplied");
      return args[1] === "acquire"
        ? { artifactDigest: artifact, verification: { valid: true } }
        : { artifactDigest: artifact, exitCode: 0 };
    },
  });
  assert.equal(calls[0].includes("--artifact"), true);
  assert.equal(calls[1].includes("--archive"), true);
});

test("ensureJatRuntime surfaces RCC artifact incompatibility without fallback build", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-incompatibility-test-"));
  const rccBinary = Buffer.from("managed-rcc-binary");
  const jatArchive = Buffer.from("rcca-archive");
  const artifact = "sha256:" + "c".repeat(64);
  const manifest = manifestFor(rccBinary, {
    jat: {
      git_sha: "d".repeat(40),
      source_archive: { asset: "source.tar.gz", url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + "d".repeat(40), sha256: "e".repeat(64) },
      environment_artifact: {
        digest: artifact,
        archive: { asset: "jat-runtime.rcca", url: "https://github.com/joshyorko/josh-all-the-things/releases/download/v0.1.1/jat-runtime.rcca", sha256: digest(jatArchive), size: jatArchive.length },
      },
    },
  });
  const calls = [];
  await assert.rejects(
    require("./runtime").ensureJatRuntime(context(root), manifest, { executable: `${root}/runtime/rcc`, version: "v18.19.2" }, {
      platform: "linux-x64",
      download: async (_url, destination) => fs.writeFileSync(destination, jatArchive),
      ensureSource: async () => path.join(root, "jat-source"),
      runJson: async (_executable, args) => {
        calls.push(args);
        if (args[1] === "acquire") return { artifactDigest: artifact, verification: { valid: false }, error: "reject incompatible environment artifact [os-version]: os.minimumVersion requires 7.1.8, worker has 5.14.0" };
        throw new Error("fallback build must not run");
      },
    }),
    /reject incompatible environment artifact \[os-version\].*requires 7\.1\.8.*worker has 5\.14\.0/,
  );
  assert.equal(calls.length, 1);
  assert.match(calls[0][0], /^env$/);
});

test("ensureControllerRuntime acquires a separate controller artifact and rejects missing metadata", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-controller-artifact-test-"));
  const rccBinary = Buffer.from("managed-rcc-binary");
  const archive = Buffer.from("controller-rcca");
  const digestValue = "sha256:" + "f".repeat(64);
  const manifest = manifestFor(rccBinary, {
    controller: {
      environment_artifact: {
        digest: digestValue,
        archive: {
          asset: "josh-room-controller.rcca",
          url: "https://github.com/joshyorko/josh-room/releases/download/v0.1.6/josh-room-controller.rcca",
          sha256: digest(archive),
          size: archive.length,
        },
      },
    },
  });
  const calls = [];
  const result = await require("./runtime").ensureControllerRuntime(
    context(root), manifest, { executable: `${root}/runtime/rcc`, version: "v18.19.2" }, {
      platform: "linux-x64",
      download: async (_url, destination) => fs.writeFileSync(destination, archive),
      runJson: async (_executable, args) => {
        calls.push(args);
        return { artifactDigest: digestValue, verification: { valid: true } };
      },
    },
  );
  assert.equal(result.artifact, digestValue);
  assert.equal(calls[0][0], "env");
  assert.equal(calls[0][1], "acquire");
  assert.equal(calls[0].includes("--archive"), true);
  await assert.rejects(
    require("./runtime").ensureControllerRuntime(
      context(fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-controller-no-pin-"))),
      manifestFor(rccBinary),
      { executable: `${root}/runtime/rcc`, version: "v18.19.2" },
      { platform: "linux-x64", runJson: async () => ({}) },
    ),
    /separate Josh Room controller environment artifact pin/,
  );
});

test("cached controller archive reuses local artifact digest without archive import", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-cached-controller-runtime-"));
  const archive = Buffer.from("cached-controller-rcca");
  const artifact = "sha256:" + "d".repeat(64);
  const asset = "josh-room-controller-linux-amd64.rcca";
  const artifactRoot = path.join(root, "runtime", "controller-artifact");
  fs.mkdirSync(artifactRoot, { recursive: true });
  fs.writeFileSync(path.join(artifactRoot, asset), archive);
  const manifest = manifestFor(Buffer.from("rcc"), {
    controller: {
      environment_artifact: {
        digest: artifact,
        archive: { asset, url: "https://github.com/joshyorko/josh-room/releases/download/test/" + asset, sha256: digest(archive), size: archive.length },
      },
    },
  });
  const calls = [];
  await ensureControllerRuntime(context(root), manifest, { executable: "/managed/rcc", version: "v18.19.3" }, {
    runJson: async (_executable, args) => {
      calls.push(args);
      return { artifactDigest: artifact, verification: { valid: true }, cacheHit: "local-materialization" };
    },
  });
  assert.equal(calls[0].includes("--artifact"), true);
  assert.equal(calls[0].includes("--archive"), false);
  assert.equal(calls[0].includes(artifact), true);
});

test("ensureJatSource rejects a non-official JAT source URL before download", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-source-url-test-"));
  const sha = "d".repeat(40);

  await assert.rejects(
    require("./runtime").ensureJatSource(context(root), {
      git_sha: sha,
      source_archive: {
        asset: "josh-all-the-things.tar.gz",
        url: "https://github.com/joshyorko/josh-all-the-things/archive/" + sha + ".tar.gz",
        sha256: "e".repeat(64),
      },
    }, {
      download: async () => {
        throw new Error("download attempted");
      },
    }),
    /official JAT source URL/,
  );
});

test("ensureJatRuntime rejects a downloaded archive with the pinned size mismatch", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-size-test-"));
  const rccBinary = Buffer.from("managed-rcc-binary");
  const jatArchive = Buffer.from("rcca-archive");
  const manifest = manifestFor(rccBinary, {
    jat: {
      git_sha: "d".repeat(40),
      source_archive: { asset: "source.tar.gz", url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + "d".repeat(40), sha256: "e".repeat(64) },
      environment_artifact: {
        digest: "sha256:" + "c".repeat(64),
        archive: {
          asset: "jat-runtime.rcca",
          url: "https://github.com/joshyorko/jat/releases/download/v0.1.1/jat-runtime.rcca",
          sha256: digest(jatArchive),
          size: jatArchive.length + 1,
        },
      },
    },
  });

  await assert.rejects(
    require("./runtime").ensureJatRuntime(
      context(root),
      manifest,
      { executable: `${root}/runtime/rcc`, version: "v18.19.2" },
      {
        platform: "linux-x64",
        download: async (_url, destination) => fs.writeFileSync(destination, jatArchive),
        ensureSource: async () => path.join(root, "jat-source"),
        runJson: async () => ({ artifactDigest: "sha256:" + "c".repeat(64), verification: { valid: true }, exitCode: 0 }),
      },
    ),
    /size mismatch/,
  );
});

test("ensureJatSource extracts a verified GitHub source archive without host tar", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-source-test-"));
  const sha = "d".repeat(40);
  const archive = zlib.gzipSync(Buffer.concat([
    tarMember(`josh-all-the-things-${sha}/robot.yaml`, "tasks:\n  Doctor:\n"),
    tarMember(`josh-all-the-things-${sha}/src/jat/__init__.py`, "__version__ = 'test'\n"),
    Buffer.alloc(1024),
  ]));
  const jat = {
    git_sha: sha,
    source_archive: {
      asset: "jat-source.tar.gz",
      url: "https://api.github.com/repos/joshyorko/josh-all-the-things/tarball/" + sha,
      sha256: digest(archive),
    },
  };
  const runtime = require("./runtime");
  const sourceRoot = await runtime.ensureJatSource(context(root), jat, {
    download: async (_url, destination) => fs.writeFileSync(destination, archive),
  });

  assert.equal(sourceRoot, path.join(root, "runtime", "jat", sha));
  assert.equal(fs.readFileSync(path.join(sourceRoot, "robot.yaml"), "utf8"), "tasks:\n  Doctor:\n");
  assert.equal(fs.readFileSync(path.join(sourceRoot, "src/jat/__init__.py"), "utf8"), "__version__ = 'test'\n");
  assert.equal(fs.readFileSync(path.join(sourceRoot, ".josh-room-source"), "utf8").trim(), sha);
});
