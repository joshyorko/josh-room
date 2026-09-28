#!/usr/bin/env node
"use strict";

const childProcess = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const fsp = fs.promises;
const repository = path.resolve(__dirname, "..");

function run(command, args, options = {}) {
  return childProcess.execFileSync(command, args, {
    cwd: repository,
    stdio: "inherit",
    ...options,
  });
}

async function expectRejected(label, operation, pattern) {
  try {
    await operation();
  } catch (error) {
    if (!pattern.test(String(error?.message || error))) throw new Error(`${label} failed with an unexpected error: ${error.message}`);
    console.log(`${label}: rejected`);
    return;
  }
  throw new Error(`${label} unexpectedly succeeded`);
}

async function main() {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), "josh-room-managed-runtime-"));
  try {
    const candidate = path.join(root, "josh-room.vsix");
    const installed = path.join(root, "installed");
    const extension = path.join(installed, "extension");
    const npm = process.platform === "win32" ? "npm.cmd" : "npm";
    run(npm, ["run", "package", "--", "--out", candidate], { cwd: path.join(repository, "vscode-extension") });
    await fsp.mkdir(installed, { recursive: true, mode: 0o700 });
    run("tar", ["-xf", candidate, "-C", installed]);

    const runtime = require(path.join(extension, "runtime.js"));
    const manifest = runtime.readManifest();
    const platform = runtime.resolvePlatform();
    const storage = path.join(root, "consumer-storage");
    const context = { globalStorageUri: { fsPath: storage }, extensionPath: extension };
    const events = [];
    const options = { platform, onProgress: (event) => events.push(event) };

    const rcc = await runtime.ensureManagedRcc(context, manifest, options);
    const controller = await runtime.ensureControllerRuntime(context, manifest, rcc, options);
    const jat = await runtime.ensureJatRuntime(context, manifest, rcc, options);

    const controllerPin = runtime.selectControllerArtifact(manifest.controller, platform);
    const paths = runtime.privatePaths(context);
    const controllerRoot = path.join(extension, "runtime", "controller");
    const receipt = path.join(paths.logsRoot, "managed-controller-acceptance.json");
    const environment = {
      ...process.env,
      ROBOCORP_HOME: paths.rccHome,
      RCC_HOLOTREE_MODE: "private",
      JOSH_ROOM_EXTENSION_MODE: "1",
      JOSH_ROOM_RCC_HOME: paths.rccHome,
      JOSH_ROOM_RCC_EXE: rcc.executable,
      JOSH_ROOM_CONTROLLER_ROOT: controllerRoot,
      JOSH_ROOM_CONTROLLER_ARTIFACT: controller.artifact,
      JOSH_ROOM_JAT_ROOT: jat.jatRoot,
      JOSH_ROOM_JAT_ARTIFACT: jat.artifact,
      PYTHONPATH: controllerRoot,
    };
    childProcess.execFileSync(rcc.executable, [
      "--no-build", "env", "exec", "--artifact", controllerPin.digest, "--permissive-local",
      "--inherit-streams", "--receipt-file", receipt, "--", "python", "-m", "josh_room", "dimensions", "list", "--json",
    ], { cwd: controllerRoot, env: environment, stdio: ["ignore", "pipe", "pipe"], encoding: "utf8", maxBuffer: 4 * 1024 * 1024 });

    const beforeWarm = events.length;
    const unavailableProvider = async () => { throw new Error("provider must not be contacted after acquisition"); };
    await runtime.ensureControllerRuntime(context, manifest, rcc, { ...options, download: unavailableProvider });
    await runtime.ensureJatRuntime(context, manifest, rcc, { ...options, download: unavailableProvider });
    if (events.slice(beforeWarm).some((event) => /Downloading|Importing/.test(event.message || ""))) {
      throw new Error("warm managed-runtime reuse attempted a provider download");
    }

    const corruptContext = { globalStorageUri: { fsPath: path.join(root, "corrupt-storage") }, extensionPath: extension };
    await expectRejected(
      "corrupt controller archive",
      () => runtime.ensureControllerRuntime(corruptContext, manifest, rcc, {
        platform,
        download: async (_url, destination) => fsp.writeFile(destination, Buffer.from("corrupt archive")),
      }),
      /size mismatch|checksum mismatch/,
    );

    const wrongRccContext = { globalStorageUri: { fsPath: path.join(root, "wrong-rcc-storage") }, extensionPath: extension };
    await expectRejected(
      "wrong RCC binary",
      () => runtime.ensureManagedRcc(wrongRccContext, manifest, {
        platform,
        download: async (_url, destination) => fsp.copyFile(rcc.executable, destination),
        verifyVersion: async () => { throw new Error("wrong RCC binary version"); },
      }),
      /wrong RCC binary version/,
    );

    const staleContext = { globalStorageUri: { fsPath: path.join(root, "stale-storage") }, extensionPath: extension };
    const stale = {
      mode: "local-build-fallback",
      extension_version: "0.1.24",
      rcc_version: manifest.rcc.version,
      platform,
      jat_source_sha: "0".repeat(40),
      jat_artifact_digest: "sha256:" + "0".repeat(64),
      controller_source_version: "0".repeat(64),
      controller_artifact_digest: "unpublished",
    };
    await runtime.writeLocalFallbackRecord(staleContext, stale);
    if (runtime.localFallbackRecordMatches(runtime.readLocalFallbackRecord(staleContext), {
      ...stale,
      extension_version: manifest.extension_version,
      jat_source_sha: jat.sourceSha,
      jat_artifact_digest: jat.artifact,
      controller_artifact_digest: controller.artifact,
    })) throw new Error("stale runtime receipt was accepted");

    console.log(JSON.stringify({
      result: "managed-runtime-consumer-pass",
      platform,
      vsix: candidate,
      rcc: { version: rcc.version, executable: rcc.executable },
      controller: controller.artifact,
      jat: { artifact: jat.artifact, source: jat.sourceSha },
      checks: ["cold-acquire", "warm-no-build", "provider-unavailable-after-acquire", "corrupt-archive-rejection", "wrong-rcc-rejection", "stale-receipt-rejection", "controller-cli", "jat-env-exec"],
    }, null, 2));
  } finally {
    await fsp.rm(root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exitCode = 1;
});
