const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const zlib = require("node:zlib");

const { ensureManagedRcc, localFallbackReason, readManifest, resolvePlatform, runtimeEnvironment } = require("./runtime");

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

test("resolvePlatform accepts only the first supported Linux mapping", () => {
  assert.equal(resolvePlatform("linux", "x64"), "linux-x64");
  assert.equal(resolvePlatform("win32", "x64"), "win32-x64");
  assert.throws(() => resolvePlatform("linux", "arm64"), /not supported/);
  assert.throws(() => resolvePlatform("darwin", "x64"), /does not support macOS yet/);
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
  assert.equal(api.localFallbackRecordMatches(api.readLocalFallbackRecord(runtimeContext), { ...expected, extension_version: "0.1.9" }), false);
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
  const expected = { schema_version: 1, mode: "local-build-fallback", extension_version: "0.1.9", platform: "linux-x64" };
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
