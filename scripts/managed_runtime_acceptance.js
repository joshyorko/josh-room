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

function evidenceDirectory(platform) {
  const configured = process.env.JOSH_ROOM_RUNTIME_EVIDENCE_DIR || path.join("dist", "managed-runtime-evidence", platform);
  return path.isAbsolute(configured) ? configured : path.join(repository, configured);
}

async function fileIdentity(runtime, filename) {
  const stat = await fsp.stat(filename);
  return { sha256: await runtime.sha256File(filename), size: stat.size };
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
  let evidence;
  try {
    const candidate = path.join(root, "josh-room.vsix");
    const installed = path.join(root, "installed");
    const extension = path.join(installed, "extension");
    if (process.platform === "win32") {
      const packageCommand = `npm run package -- --out "${candidate.replaceAll("\"", "\\\"")}"`;
      run(process.env.ComSpec || "cmd.exe", ["/d", "/s", "/c", packageCommand], { cwd: path.join(repository, "vscode-extension") });
    } else {
      run("npm", ["run", "package", "--", "--out", candidate], { cwd: path.join(repository, "vscode-extension") });
    }
    await fsp.mkdir(installed, { recursive: true, mode: 0o700 });
    if (process.platform === "win32") {
      run("tar", ["-xf", candidate, "-C", installed]);
    } else {
      run("unzip", ["-q", candidate, "-d", installed]);
    }

    const runtime = require(path.join(extension, "runtime.js"));
    const manifest = runtime.readManifest();
    const platform = runtime.resolvePlatform();
    const evidenceDir = evidenceDirectory(platform);
    await fsp.rm(evidenceDir, { recursive: true, force: true });
    await fsp.mkdir(evidenceDir, { recursive: true, mode: 0o700 });
    await fsp.copyFile(candidate, path.join(evidenceDir, "candidate.vsix"));

    const lock = JSON.parse(await fsp.readFile(path.join(repository, "release-lock.json"), "utf8"));
    const storage = path.join(root, "consumer-storage");
    const context = { globalStorageUri: { fsPath: storage }, extensionPath: extension };
    const events = [];
    const options = { platform, onProgress: (event) => events.push(event) };

    const rcc = await runtime.ensureManagedRcc(context, manifest, options);
    const controller = await runtime.ensureControllerRuntime(context, manifest, rcc, options);
    const jat = await runtime.ensureJatRuntime(context, manifest, rcc, options);

    const controllerPin = runtime.selectControllerArtifact(manifest.controller, platform);
    const jatPin = runtime.selectJatArtifact(manifest.jat, platform);
    const paths = runtime.privatePaths(context);
    const controllerRoot = path.join(extension, "runtime", "controller");
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
    const executeController = (args, name) => {
      const receipt = path.join(paths.logsRoot, `managed-controller-${name}.json`);
      return childProcess.execFileSync(rcc.executable, [
        "--no-build", "env", "exec", "--artifact", controllerPin.digest, "--permissive-local",
        "--inherit-streams", "--receipt-file", receipt, "--", "python", "-m", "josh_room", ...args,
      ], { cwd: controllerRoot, env: environment, stdio: ["ignore", "pipe", "pipe"], encoding: "utf8", maxBuffer: 4 * 1024 * 1024 });
    };

    const runManagedTool = (args, name) => {
      const receipt = path.join(paths.logsRoot, `managed-tool-${name}.json`);
      return childProcess.execFileSync(rcc.executable, [
        "--no-build", "env", "exec", "--artifact", controllerPin.digest, "--permissive-local",
        "--inherit-streams", "--receipt-file", receipt, "--", ...args,
      ], { cwd: root, env: environment, stdio: ["ignore", "pipe", "pipe"], encoding: "utf8", maxBuffer: 4 * 1024 * 1024 });
    };
    const identityBodies = [];
    const recipients = [];
    for (const name of ["primary", "recovery"]) {
      const identityPath = path.join(root, `${name}-age-identity.txt`);
      runManagedTool(["age-keygen", "-o", identityPath], `age-keygen-${name}`);
      const identityBody = await fsp.readFile(identityPath, "utf8");
      const recipient = identityBody.match(/^# public key: (age1\S+)$/m)?.[1];
      if (!recipient) throw new Error(`age-keygen did not return a recipient for ${name}`);
      identityBodies.push(identityBody.trimEnd());
      recipients.push(recipient);
    }
    const identityPath = path.join(root, "age-identity.txt");
    await fsp.writeFile(identityPath, `${identityBodies.join("\n")}\n`, { mode: 0o600 });
    const instance = path.join(root, "room-instance");
    const workspaceRoot = path.join(root, "workspaces");
    Object.assign(environment, {
      JOSH_ROOM_CONFIG_DIR: path.join(root, "config"),
      JOSH_ROOM_IDENTITY: identityPath,
      JOSH_ROOM_INSTANCE: instance,
      JOSH_ROOM_RECIPIENTS: recipients.join(","),
      JOSH_ROOM_WORKSPACE_ROOT: workspaceRoot,
    });
    const source = path.join(root, "save-source");
    await fsp.mkdir(source, { recursive: true, mode: 0o700 });
    await fsp.writeFile(path.join(source, "README.md"), "managed runtime Save/Enter acceptance\n");
    executeController(["dimensions", "list", "--json"], "dimensions-list");
    executeController(["snapshot", "create", "demo", "--source", source, "--backend", "local", "--json"], "save");
    executeController(["enter", "demo", "--snapshot", "latest", "--backend", "local", "--ide", "terminal", "--json"], "enter");
    const restored = path.join(workspaceRoot, "demo", "README.md");
    if ((await fsp.readFile(restored, "utf8")) !== "managed runtime Save/Enter acceptance\n") {
      throw new Error("managed runtime Enter did not restore the saved workspace");
    }
    executeController(["jat", "inspect", "--help"], "jat-command");

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

    const rccPin = manifest.rcc.platforms[platform];
    evidence = {
      result: "managed-runtime-consumer-pass",
      platform,
      source_sha: lock.controller.source_sha,
      vsix: { asset: "candidate.vsix", ...(await fileIdentity(runtime, candidate)), extension_version: manifest.extension_version },
      rcc: { version: rcc.version, source_sha: lock.rcc.source_sha, asset: rccPin.asset, ...(await fileIdentity(runtime, rcc.executable)) },
      controller: {
        release_tag: lock.controller.release_tag,
        artifact_digest: controllerPin.digest,
        specification_digest: controllerPin.specification_digest,
        archive: { asset: controllerPin.archive.asset, ...(await fileIdentity(runtime, controller.archive)) },
        source_sha: lock.controller.source_sha,
        rcc_version: rcc.version,
      },
      jat: {
        release_tag: lock.jat.environment_artifact.release_tag,
        artifact_digest: jatPin.digest,
        specification_digest: jatPin.specification_digest,
        archive: { asset: jatPin.archive.asset, ...(await fileIdentity(runtime, jat.archive)) },
        source_sha: jat.sourceSha,
        rcc_version: rcc.version,
      },
      checks: ["clean-installed-vsix", "cold-acquire", "warm-no-build", "provider-unavailable-after-acquire", "corrupt-archive-rejection", "wrong-rcc-rejection", "stale-receipt-rejection", "controller-cli", "save", "enter", "jat-env-exec", "jat-command-path"],
    };
    if (fs.existsSync(path.join(paths.logsRoot, "jat-artifact-receipt.json"))) {
      await fsp.copyFile(path.join(paths.logsRoot, "jat-artifact-receipt.json"), path.join(evidenceDir, "jat-artifact-receipt.json"));
    }
    for (const filename of fs.existsSync(paths.logsRoot) ? fs.readdirSync(paths.logsRoot) : []) {
      if ((filename.startsWith("managed-controller-") || filename.startsWith("managed-tool-")) && filename.endsWith(".json")) {
        await fsp.copyFile(path.join(paths.logsRoot, filename), path.join(evidenceDir, filename));
      }
    }
    await fsp.writeFile(path.join(evidenceDir, "evidence.json"), `${JSON.stringify(evidence, null, 2)}\n`, { mode: 0o600 });
    console.log(JSON.stringify(evidence, null, 2));
  } finally {
    await fsp.rm(root, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exitCode = 1;
});
