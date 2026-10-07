#!/usr/bin/env node
"use strict";

const childProcess = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");
const fsp = fs.promises;
const repository = path.resolve(__dirname, "..");
const { compareRestoredFile, jatRestoredWorkspace } = require("./managed_runtime_acceptance_helpers");

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

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = address && typeof address === "object" ? address.port : 0;
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  if (!port) throw new Error("managed JAT serve did not receive a TCP port");
  return port;
}

async function waitForPort(port, timeoutMs = 120000, state = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (state.error) throw state.error;
    if ((state.exitCode !== undefined && state.exitCode !== null) || state.signal) {
      const diagnostics = [state.stdout, state.stderr].filter(Boolean).join("\n").slice(-8192);
      throw new Error(`managed JAT serve exited with code ${state.exitCode ?? "unknown"}${state.signal ? ` (${state.signal})` : ""}${diagnostics ? `: ${diagnostics}` : ""}`);
    }
    const connected = await new Promise((resolve) => {
      const socket = net.createConnection({ host: "127.0.0.1", port });
      socket.once("connect", () => {
        socket.destroy();
        resolve(true);
      });
      socket.once("error", () => {
        socket.destroy();
        resolve(false);
      });
    });
    if (connected) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const diagnostics = [state.stdout, state.stderr].filter(Boolean).join("\n").slice(-8192);
  throw new Error(`managed JAT serve did not open port ${port}${diagnostics ? `: ${diagnostics}` : ""}`);
}

async function stopManagedProcess(child, closed) {
  const waitClosed = (timeoutMs) => Promise.race([
    closed,
    new Promise((resolve) => setTimeout(resolve, timeoutMs)),
  ]);
  if (child.exitCode === null) {
    if (process.platform === "win32") {
      await new Promise((resolve) => {
        const killer = childProcess.spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          stdio: "ignore",
          windowsHide: true,
        });
        killer.once("error", resolve);
        killer.once("close", resolve);
      });
    } else if (child.pid) {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch (error) {
        if (error.code !== "ESRCH") child.kill("SIGTERM");
      }
    }
  }
  await waitClosed(5000);
  if (child.exitCode === null) {
    if (process.platform === "win32") child.kill();
    else if (child.pid) {
      try {
        process.kill(-child.pid, "SIGKILL");
      } catch (error) {
        if (error.code !== "ESRCH") child.kill("SIGKILL");
      }
    }
  }
  child.stdout?.destroy();
  child.stderr?.destroy();
  await waitClosed(1000);
  if (child.exitCode === null) child.unref();
}

async function main() {
  const root = await fsp.realpath(await fsp.mkdtemp(path.join(os.tmpdir(), "josh-room-managed-runtime-")));
  try {
    const candidate = path.join(root, "josh-room.vsix");
    const installed = path.join(root, "installed");
    const extension = path.join(installed, "extension");
    if (process.platform === "win32") {
      run("npm.cmd", ["run", "package", "--", "--out", candidate], {
        cwd: path.join(repository, "vscode-extension"),
        shell: true,
      });
    } else {
      run("npm", ["run", "package", "--", "--out", candidate], { cwd: path.join(repository, "vscode-extension") });
    }
    if (!fs.existsSync(candidate)) throw new Error(`VSIX packaging did not create ${candidate}`);
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
      JOSH_ROOM_EXTENSION_VERSION: manifest.extension_version,
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
    const readManagedResult = (filename) => {
      if (!filename || !fs.existsSync(filename)) return null;
      try {
        const result = JSON.parse(fs.readFileSync(filename, "utf8"));
        return result && typeof result === "object" ? result : null;
      } catch {
        return null;
      }
    };
    const runManaged = (args, prefix, name, cwd, { inheritStreams = true, resultFile = null } = {}) => {
      const receipt = path.join(paths.logsRoot, `${prefix}-${name}.json`);
      const invocationEnvironment = { ...environment };
      if (resultFile) {
        fs.rmSync(resultFile, { force: true });
        fs.mkdirSync(path.dirname(resultFile), { recursive: true, mode: 0o700 });
        invocationEnvironment.JOSH_ROOM_RESULT_FILE = resultFile;
      }
      const command = ["--no-build", "env", "exec", "--artifact", controllerPin.digest, "--permissive-local"];
      if (inheritStreams) command.push("--inherit-streams", "--receipt-file", receipt);
      else command.push("--json");
      command.push("--", ...args);
      try {
        const output = childProcess.execFileSync(rcc.executable, command, { cwd, env: invocationEnvironment, stdio: ["ignore", "pipe", "pipe"], encoding: "utf8", maxBuffer: 4 * 1024 * 1024 });
        if (resultFile && !readManagedResult(resultFile)) {
          throw new Error(`${prefix}-${name} did not produce a valid Josh Room result`);
        }
        return output;
      } catch (error) {
        if (prefix === "managed-tool" && name === "private-runtime") {
          for (const line of String(error?.stdout || "").split(/\r?\n/)) {
            try {
              const probe = JSON.parse(line);
              if (probe.status !== "failed") continue;
              const safe = {};
              for (const key of ["status", "platform", "failed_check", "error_type", "winerror", "checks_completed"]) {
                if (Object.hasOwn(probe, key)) safe[key] = probe[key];
              }
              process.stderr.write(`[managed-private-runtime-result] ${JSON.stringify(safe)}\n`);
            } catch { /* Ignore non-probe RCC output. */ }
          }
        }
        const result = readManagedResult(resultFile);
        if (result) {
          process.stderr.write(`[${prefix}-${name}-result] ${JSON.stringify(result).slice(0, 8192)}\n`);
        }
        const value = error?.stderr;
        if (value) {
          const text = Buffer.isBuffer(value) ? value.toString("utf8") : String(value);
          process.stderr.write(`[${prefix}-${name}-stderr] ${text.slice(-8192)}\n`);
        }
        throw error;
      }
    };
    const executeController = (args, name) => runManaged(
      ["python", "-m", "josh_room", ...args],
      "managed-controller",
      name,
      controllerRoot,
      { inheritStreams: false, resultFile: path.join(paths.logsRoot, `managed-controller-${name}-result.json`) },
    );
    const runManagedTool = (args, name) => runManaged(args, "managed-tool", name, root);
    const runManagedJatServe = async (haul, name) => {
      const port = await reservePort();
      const jatEnvironment = {
        ...environment,
        JAT_RUN_DIR: path.join(root, `jat-run-${name}`),
        PYTHONPATH: [path.join(jat.jatRoot, "src"), jat.jatRoot, controllerRoot].join(path.delimiter),
      };
      const child = childProcess.spawn(rcc.executable, [
        "--no-build", "env", "exec", "--artifact", jatPin.digest, "--permissive-local", "--json", "--",
        "python", "-m", "jat.cli", "serve", "--haul", haul, "--mode", "files",
        "--fileserver-port", String(port), "--json",
      ], {
        cwd: root,
        env: jatEnvironment,
        detached: process.platform !== "win32",
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
      const state = { error: null, exitCode: null, signal: null, stdout: "", stderr: "" };
      const capture = (key, chunk) => {
        state[key] = `${state[key]}${Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk)}`.slice(-8192);
      };
      child.stdout.on("data", (chunk) => capture("stdout", chunk));
      child.stderr.on("data", (chunk) => capture("stderr", chunk));
      child.stdout.resume();
      child.stderr.resume();
      child.once("error", (error) => { state.error = error; });
      const closed = new Promise((resolve) => child.once("close", (code, signal) => {
        state.exitCode = code;
        state.signal = signal;
        resolve();
      }));
      try {
        await waitForPort(port, 120000, state);
      } finally {
        await stopManagedProcess(child, closed);
      }
      return { port };
    };
    const identityBodies = [];
    const recipients = [];
    for (const name of ["primary", "recovery"]) {
      const identityPath = path.join(root, `${name}-age-identity.txt`);
      const recipient = runManagedTool([
        "python", "-c",
        "import sys; from pathlib import Path; from josh_room.crypto import generate_identity, derive_recipient; path = Path(sys.argv[1]); generate_identity(path); print(derive_recipient(path))",
        identityPath,
      ], `age-identity-${name}`).trim();
      if (!/^age1[0-9a-z]{58}$/.test(recipient)) throw new Error(`managed age identity generation returned an invalid recipient for ${name}`);
      const identityBody = await fsp.readFile(identityPath, "utf8");
      identityBodies.push(identityBody.trimEnd());
      recipients.push(recipient);
    }
    const dependencyProbe = runManagedTool([
      "python", "-c", "import boto3, josh_room; print(boto3.__version__)",
    ], "dependencies").trim();
    if (!dependencyProbe) throw new Error("managed controller dependency probe returned no boto3 version");
    const privateRuntime = JSON.parse(runManagedTool([
      "python", path.join(repository, "scripts", "verify_private_runtime.py"),
    ], "private-runtime").trim());
    if (privateRuntime.status !== "passed") {
      throw new Error("managed private runtime protection did not pass");
    }
    const resticInstallation = JSON.parse(runManagedTool([
      "python", path.join(controllerRoot, "install_restic.py"),
      "--manifest", path.join(extension, "runtime", "restic-manifest.json"),
      "--destination", paths.runtimeRoot, "--platform", platform,
    ], "restic-install").trim());
    const resticPhase0 = JSON.parse(runManagedTool([
      "python", path.join(repository, "scripts", "room_store_probe.py"),
      "--restic", resticInstallation.executable,
      "--evidence-file", path.join(evidenceDir, "restic-phase0.json"),
    ], "restic-phase0").trim());
    if (resticPhase0.status !== "passed" || resticPhase0.engine_version !== "0.19.1") {
      throw new Error("managed restic feasibility did not pass on the selected platform");
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
    // JAT extracts Build archives into <payload_path>/workspace and keeps the source basename.
    const source = path.join(root, "workspace");
    await fsp.mkdir(source, { recursive: true, mode: 0o700 });
    const sourceReadme = path.join(source, "README.md");
    await fsp.writeFile(sourceReadme, "managed runtime legacy local JAT acceptance\n");
    await fsp.chmod(sourceReadme, 0o444);
    const sourceReadmeBytes = await fsp.readFile(sourceReadme);
    const sourceReadmeMode = (await fsp.stat(sourceReadme)).mode;
    if (platform === "linux-x64") {
      await fsp.copyFile(
        path.join(jat.jatRoot, "environment_linux_amd64_freeze.yaml"),
        path.join(source, "environment_linux_amd64_freeze.yaml"),
      );
      await fsp.writeFile(path.join(source, "robot.yaml"),
        "tasks:\n  Example:\n    shell: python -c \"print('synthetic')\"\n"
        + "environmentConfigs:\n  - environment_linux_amd64_freeze.yaml\n");
    }
    executeController(["dimensions", "list", "--json"], "dimensions-list");
    executeController(["snapshot", "create", "demo", "--source", source, "--backend", "local", "--json"], "legacy-local-jat-save");
    executeController(["enter", "demo", "--snapshot", "latest", "--backend", "local", "--ide", "terminal", "--json"], "legacy-local-jat-enter");
    const restored = path.join(workspaceRoot, "demo", "README.md");
    const legacyEnterComparison = compareRestoredFile({
      sourceBytes: sourceReadmeBytes,
      restoredBytes: await fsp.readFile(restored),
      sourceMode: sourceReadmeMode,
      restoredMode: (await fsp.stat(restored)).mode,
      platform,
    });
    if (!legacyEnterComparison.bytes_match || !legacyEnterComparison.mode_match) {
      throw new Error("legacy local JAT Enter did not restore the saved workspace");
    }
    const haul = path.join(root, "managed-runtime.haul.tar.zst");
    executeController(["jat", "build", "--source", source, "--output", haul, "--json"], "jat-build");
    const captureRobot = (folder, output, name) => runManagedTool([
      "python", "-c",
      "import json, sys; from pathlib import Path; from josh_room.jat import run_build; "
      + "result = run_build(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), rcc_environment='required'); "
      + "print(json.dumps({'success': result['success'], 'exit_status': result['exit_status'], "
      + "'rcc_component_captured': isinstance(result.get('environment_artifact'), dict)}))",
      jat.jatRoot, folder, output,
    ], name);
    const componentHaul = platform === "linux-x64" ? path.join(root, "managed-runtime-with-rcc.haul.tar.zst") : haul;
    if (platform === "linux-x64") {
      captureRobot(source, componentHaul, "jat-build-with-rcc");
    }
    executeController(["jat", "inspect", "--haul", componentHaul, "--json"], "jat-inspect");
    const componentInventory = readManagedResult(path.join(paths.logsRoot, "managed-controller-jat-inspect-result.json"));
    if (platform === "linux-x64" && componentInventory?.anchors?.rcc_environment !== true) {
      throw new Error("managed robot workspace capture omitted its RCC component");
    }
    if (platform === "linux-x64") {
      const largerSource = path.join(root, "larger-workspace");
      await fsp.mkdir(largerSource, { mode: 0o700 });
      for (const name of ["robot.yaml", "environment_linux_amd64_freeze.yaml"]) {
        await fsp.copyFile(path.join(source, name), path.join(largerSource, name));
      }
      for (let index = 0; index < 2048; index += 1) {
        await fsp.writeFile(path.join(largerSource, `entry-${index}.txt`), `synthetic entry ${index}\n`);
      }
      const largerHaul = path.join(root, "larger-workspace.haul.tar.zst");
      captureRobot(largerSource, largerHaul, "jat-build-larger");
      executeController(["jat", "inspect", "--haul", largerHaul, "--json"], "jat-inspect-larger");
      if (readManagedResult(path.join(paths.logsRoot, "managed-controller-jat-inspect-larger-result.json"))?.anchors?.rcc_environment !== true) {
        throw new Error("larger managed robot workspace capture omitted its RCC component");
      }
    }
    const legacyJatRestore = path.join(root, "legacy-jat-clean-room");
    executeController([
      "jat", "restore", "--haul", haul, "--destination", legacyJatRestore, "--json",
    ], "legacy-jat-clean-room-restore");
    const legacyJatRestoreResultFile = path.join(paths.logsRoot, "managed-controller-legacy-jat-clean-room-restore-result.json");
    const legacyJatRestoreResult = readManagedResult(legacyJatRestoreResultFile);
    const restoredWorkspaceRoot = jatRestoredWorkspace(
      legacyJatRestoreResult,
      legacyJatRestore,
      path.basename(source),
      platform,
    );
    const cleanReadme = restoredWorkspaceRoot && path.join(restoredWorkspaceRoot, "README.md");
    let cleanReadmeBytes = Buffer.alloc(0);
    let cleanReadmeMode = 0;
    let cleanReadmeExists = false;
    if (cleanReadme) {
      try {
        cleanReadmeBytes = await fsp.readFile(cleanReadme);
        cleanReadmeMode = (await fsp.stat(cleanReadme)).mode;
        cleanReadmeExists = true;
      } catch { /* Record the missing or unreadable payload below. */ }
    }
    const legacyJatRestoreComparison = compareRestoredFile({
      sourceBytes: sourceReadmeBytes,
      restoredBytes: cleanReadmeBytes,
      sourceMode: sourceReadmeMode,
      restoredMode: cleanReadmeMode,
      restoredExists: cleanReadmeExists,
      platform,
    });
    const legacyJatRestoreReceipt = {
      operation: legacyJatRestoreResult?.operation || null,
      success: legacyJatRestoreResult?.success === true,
      exit_status: legacyJatRestoreResult?.exit_status ?? null,
      payload_path_matches_destination: restoredWorkspaceRoot !== null,
      workspace_layout: "<payload_path>/workspace/<build-source-name>",
      file: { name: "README.md", ...legacyJatRestoreComparison },
      status: legacyJatRestoreResult?.operation === "restore"
        && legacyJatRestoreResult?.success === true
        && legacyJatRestoreResult?.exit_status === 0
        && restoredWorkspaceRoot !== null
        && legacyJatRestoreComparison.bytes_match
        && legacyJatRestoreComparison.mode_match
        ? "passed"
        : "failed",
    };
    await fsp.writeFile(
      path.join(evidenceDir, "legacy-jat-clean-room-restore.json"),
      `${JSON.stringify(legacyJatRestoreReceipt, null, 2)}\n`,
      { mode: 0o600 },
    );
    if (legacyJatRestoreReceipt.status !== "passed") {
      throw new Error("existing legacy JAT capsule failed clean-room byte/mode restore");
    }
    await runManagedJatServe(haul, "jat-serve");

    let windowsRoomStoreAcceptance = { status: "not-applicable", native_windows_only: true };
    if (platform === "win32-x64") {
      const roomStoreResultFile = path.join(paths.logsRoot, "managed-controller-windows-room-store-result.json");
      runManaged([
        "python", path.join(repository, "scripts", "windows_room_store_acceptance.py"),
        "--restic", resticInstallation.executable,
        "--operational-identity", path.join(root, "primary-age-identity.txt"),
        "--recovery-identity", path.join(root, "recovery-age-identity.txt"),
        "--jat-root", jat.jatRoot,
        "--output", path.join(root, "room-store-portable.haul.tar.zst"),
      ], "managed-controller", "windows-room-store-acceptance", root, {
        inheritStreams: false,
        resultFile: roomStoreResultFile,
      });
      windowsRoomStoreAcceptance = JSON.parse(await fsp.readFile(roomStoreResultFile, "utf8"));
      if (windowsRoomStoreAcceptance.status !== "passed"
        || windowsRoomStoreAcceptance.storage?.real_minio !== false) {
        throw new Error("Windows Room Store fixture acceptance did not pass with an explicit fixture storage label");
      }
      await fsp.copyFile(roomStoreResultFile, path.join(evidenceDir, "windows-room-store-acceptance.json"));
    }

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
    const evidence = {
      result: "managed-runtime-consumer-pass",
      platform,
      source_sha: childProcess.execFileSync("git", ["rev-parse", "HEAD"], { cwd: repository, encoding: "utf8" }).trim(),
      vsix: { asset: "candidate.vsix", ...(await fileIdentity(runtime, candidate)), extension_version: manifest.extension_version },
      rcc: { version: rcc.version, source_sha: lock.rcc.source_sha, asset: rccPin.asset, ...(await fileIdentity(runtime, rcc.executable)) },
      dependencies: { boto3: dependencyProbe },
      private_runtime: privateRuntime,
      windows_room_store_acceptance: windowsRoomStoreAcceptance,
      restic: { version: resticInstallation.version, platform, ...(await fileIdentity(runtime, resticInstallation.executable)), metrics: resticPhase0.metrics },
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
      checks: ["clean-installed-vsix", "cold-acquire", "warm-no-build", "provider-unavailable-after-acquire", "corrupt-archive-rejection", "wrong-rcc-rejection", "stale-receipt-rejection", "controller-cli", "legacy-local-jat-save", "legacy-local-jat-enter", "legacy-jat-build", "legacy-jat-inspect", "legacy-jat-clean-room-restore-bytes-and-mode", "legacy-jat-serve", "jat-env-exec", "restic-package", "restic-phase0", "private-runtime", ...(platform === "win32-x64" ? ["windows-room-store-incremental-noop-rename-delete", "windows-room-store-enter", "windows-portable-jat-export-clean-room-restore", "windows-room-store-path-guards"] : [])],
    };
    for (const filename of fs.existsSync(paths.logsRoot) ? fs.readdirSync(paths.logsRoot) : []) {
      if ((filename.startsWith("managed-controller-") || filename.startsWith("managed-tool-") || filename.startsWith("managed-jat-") || filename.startsWith("jat-artifact-")) && filename.endsWith(".json")) {
        if (filename === "managed-controller-legacy-jat-clean-room-restore-result.json") continue;
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
