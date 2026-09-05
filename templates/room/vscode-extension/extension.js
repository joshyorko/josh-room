const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");
const { WorkspaceBaseline, isRoomMarker, shouldMarkDirty } = require("./dirty");
const managedRuntime = require("./runtime");
const {
  createProgressTracker,
  followProgressFile,
  formatProgressDisplay,
  operationKind,
} = require("./progress");
const { FILESERVER_URL, REGISTRY_URL, followLogFile, stageForLog, waitForFileserver, waitForRegistry } = require("./registry");
const providerTools = require("./provider");

let outputChannel;
let roomsProvider;
let statusItem;
let activeOperationId = 0;
let extensionContext;
let roomDirty = false;
let workspaceBaseline;
let workspaceWatcher;
let dirtyTrackingGeneration = 0;
const dirtyBuffers = new Set();
let activeAuthAttempt;
let managedRuntimePromise;
let managedJatPromise;
let runtimeReadinessOverride;
let runtimeLifecycle = { state: "UNINITIALIZED", message: "Preparing Josh Room runtime…" };
let testRuntime;
const CREDENTIALS_SECRET = "josh-room.credentials.v1";
const ENCRYPTION_SECRET_PREFIX = "josh-room.encryption.v1:";
const RECOVERY_SECRET_PREFIX = "josh-room.recovery.v1:";

function assertWorkspaceTrusted(action = "use Josh Room") {
  if (vscode.workspace.isTrusted === false) {
    throw new Error(`Workspace Trust is required to ${action}.`);
  }
}

function setStatus(text, tooltip = "Open Josh Room") {
  if (!statusItem) return;
  statusItem.text = text;
  statusItem.tooltip = tooltip;
}

function activeWorkspace() {
  assertWorkspaceTrusted("use Josh Room");
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("Open a workspace folder before using Josh Room.");
  }
  return folder.uri.fsPath;
}

function roomsRoot(workspace) {
  const parent = path.dirname(workspace);
  return path.basename(parent) === "workspaces" ? parent : workspace;
}

function configuredWorkspaceRoot(cwd) {
  const configured = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  if (configured && configured.trim()) return path.resolve(configured);
  const configDirectory = process.env.JOSH_ROOM_CONFIG_DIR
    || path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "josh-room");
  try {
    const config = JSON.parse(fs.readFileSync(path.join(configDirectory, "config.json"), "utf8"));
    if (typeof config.workspace_root === "string" && config.workspace_root.trim()) {
      return path.resolve(config.workspace_root);
    }
  } catch (_error) {
    // A missing or unreadable optional config only means the fallback is used.
  }
  return roomsRoot(cwd);
}

function materializationIndexPath() {
  const stateHome = process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state");
  const instance = process.env.JOSH_ROOM_INSTANCE || path.join(stateHome, "josh-room");
  return path.join(instance, "materializations.json");
}

function materializationKey(dimensionIdValue, projectId) {
  return JSON.stringify([dimensionIdValue, projectId]);
}

function readMaterializationIndex() {
  try {
    const value = JSON.parse(fs.readFileSync(materializationIndexPath(), "utf8"));
    if (value && value.materializations && typeof value.materializations === "object"
      && !Array.isArray(value.materializations)) return { ...value.materializations };
  } catch (_error) {
    // A stale or corrupt convenience index is ignored and rebuilt from markers.
  }
  return {};
}

function writeMaterializationIndex(materializations) {
  try {
    const indexPath = materializationIndexPath();
    fs.mkdirSync(path.dirname(indexPath), { recursive: true, mode: 0o700 });
    const temporary = path.join(
      path.dirname(indexPath),
      `.materializations.${process.pid}.${Date.now()}`,
    );
    fs.writeFileSync(temporary, `${JSON.stringify({ format_version: 1, materializations }, null, 2)}\n`, { mode: 0o600 });
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, indexPath);
  } catch (error) {
    outputChannel?.warn(`Unable to update the local Room locator: ${error.message}`);
  }
}

function canonicalWorkspacePathSha256(workspace) {
  return crypto.createHash("sha256").update(path.resolve(workspace)).digest("hex");
}

function corroboratedMaterialization(candidate, dimensionIdValue, projectId) {
  try {
    const stat = fs.lstatSync(candidate);
    if (!stat.isDirectory() || stat.isSymbolicLink()) return undefined;
    const marker = currentRoom(candidate);
    if (!marker || marker.format_version !== 2
      || marker.dimension_id !== dimensionIdValue
      || marker.project_id !== projectId
      || marker.workspace_path_sha256 !== canonicalWorkspacePathSha256(candidate)) return undefined;
    return { path: path.resolve(candidate), marker };
  } catch (_error) {
    return undefined;
  }
}

function findMaterialization(cwd, dimensionIdValue, projectId) {
  const key = materializationKey(dimensionIdValue, projectId);
  const index = readMaterializationIndex();
  const candidates = [];
  if (typeof index[key] === "string") candidates.push(index[key]);
  candidates.push(cwd);
  const root = configuredWorkspaceRoot(cwd);
  try {
    const stat = fs.lstatSync(root);
    if (stat.isDirectory() && !stat.isSymbolicLink()) {
      for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
        if (entry.isDirectory() && !entry.isSymbolicLink()) candidates.push(path.join(root, entry.name));
      }
    }
  } catch (_error) {
    // An absent configured root simply has no materializations to scan.
  }
  candidates.push(path.join(roomsRoot(cwd), projectId));
  const seen = new Set();
  let staleIndex = false;
  for (const candidate of candidates) {
    if (typeof candidate !== "string") continue;
    const resolved = path.resolve(candidate);
    if (seen.has(resolved)) continue;
    seen.add(resolved);
    const found = corroboratedMaterialization(resolved, dimensionIdValue, projectId);
    if (found) {
      if (index[key] !== found.path) {
        index[key] = found.path;
        writeMaterializationIndex(index);
      }
      return found;
    }
    if (index[key] === resolved) staleIndex = true;
  }
  if (staleIndex) {
    delete index[key];
    writeMaterializationIndex(index);
  }
  return undefined;
}

function rememberMaterialization(materialization) {
  const dimensionIdValue = materialization.marker.dimension_id;
  const projectId = materialization.marker.project_id;
  const index = readMaterializationIndex();
  index[materializationKey(dimensionIdValue, projectId)] = path.resolve(materialization.path);
  writeMaterializationIndex(index);
}

function newMaterializationDestination(cwd, dimensionIdValue, projectId) {
  const root = configuredWorkspaceRoot(cwd);
  const preferred = path.join(root, projectId);
  if (!fs.existsSync(preferred)) return preferred;
  const marker = currentRoom(preferred);
  if (marker && marker.format_version === 2
    && marker.project_id === projectId
    && marker.dimension_id !== dimensionIdValue) {
    return path.join(root, `${dimensionIdValue}--${projectId}`);
  }
  return preferred;
}

function rebindHydratedMarker(workspace, dimensionIdValue, projectId, snapshotId) {
  const markerPath = path.join(workspace, ".josh-room.json");
  const marker = JSON.parse(fs.readFileSync(markerPath, "utf8"));
  if (marker.format_version !== 2
    || marker.dimension_id !== dimensionIdValue
    || marker.project_id !== projectId
    || marker.snapshot_id !== snapshotId
    || !/^[0-9a-f]{64}$/.test(marker.workspace_fingerprint)) {
    throw new Error("Restored Room marker could not be corroborated for this Dimension.");
  }
  const temporary = path.join(workspace, `.josh-room-marker.${process.pid}.${Date.now()}`);
  fs.writeFileSync(temporary, `${JSON.stringify({
    ...marker,
    dimension_id: dimensionIdValue,
    project_id: projectId,
    snapshot_id: snapshotId,
    workspace_path_sha256: canonicalWorkspacePathSha256(workspace),
  })}\n`, { mode: 0o600 });
  fs.chmodSync(temporary, 0o600);
  fs.renameSync(temporary, markerPath);
  return {
    path: path.resolve(workspace),
    marker: {
      ...marker,
      dimension_id: dimensionIdValue,
      project_id: projectId,
      snapshot_id: snapshotId,
      workspace_path_sha256: canonicalWorkspacePathSha256(workspace),
    },
  };
}

async function switchMaterialization(cwd, selected, existing, provider) {
  const status = await runJoshRoom(["status", "--workspace", existing.path], cwd);
  const clean = status.state === "clean" && status.path_matches !== false && status.fingerprint_matches !== false;
  if (clean) {
    const action = await vscode.window.showWarningMessage(
      `Switch this Room to recovery point ${selected.snapshotId}?`,
      { modal: true },
      "Switch Recovery Point",
    );
    if (action !== "Switch Recovery Point") return "cancelled";
  } else {
    const action = await vscode.window.showWarningMessage(
      "This Room has local changes. Save Current First, Discard Changes & Switch, or Cancel.",
      { modal: true },
      "Save Current First",
      "Discard Changes & Switch",
      "Cancel",
    );
    if (action === "Save Current First") {
      await vscode.window.showInformationMessage("Save the current Room first, then choose the historical JAT again.");
      return "save-first";
    }
    if (action !== "Discard Changes & Switch") return "cancelled";
  }
  const parent = path.dirname(existing.path);
  const stage = fs.mkdtempSync(path.join(parent, `.${selected.projectId}.josh-room-switch-`));
  const backup = path.join(
    parent,
    `.${selected.projectId}.josh-room-backup-${process.pid}-${Date.now()}`,
  );
  let promoted = false;
  try {
    await runOperation(
      `Restoring ${selected.label}…`,
      nativeRegistry.dimensionArgs([
        "hydrate", selected.projectId,
        "--snapshot", selected.snapshotId,
        "--destination", stage,
        "--backend", provider,
        "--ide", "terminal",
      ], selected.dimensionId),
      cwd,
    );
    const finalDimensionId = selected.dimensionId
      || selected.project?.dimension_id
      || selected.project?.dimension?.id
      || selected.project?.dimension?.dimension_id;
    const finalProjectId = selected.projectId || selected.project?.id || selected.project?.project_id;
    const finalSnapshotId = selected.snapshotId || selected.snapshot_id || selected.snapshot?.snapshot_id;
    let restored;
    fs.renameSync(existing.path, backup);
    try {
      fs.renameSync(stage, existing.path);
      restored = rebindHydratedMarker(existing.path, finalDimensionId, finalProjectId, finalSnapshotId);
      promoted = true;
    } catch (error) {
      fs.rmSync(existing.path, { recursive: true, force: true });
      if (fs.existsSync(backup)) fs.renameSync(backup, existing.path);
      throw error;
    }
    fs.rmSync(backup, { recursive: true, force: true });
    rememberMaterialization(restored);
  } finally {
    if (!promoted && fs.existsSync(stage)) fs.rmSync(stage, { recursive: true, force: true });
    if (promoted && fs.existsSync(backup)) fs.rmSync(backup, { recursive: true, force: true });
  }
  return "switched";
}

function cancellationError() {
  const error = new Error("Josh Room operation cancelled");
  error.code = "ABORT_ERR";
  return error;
}

function isCancellationError(error) {
  return error?.code === "ABORT_ERR" || error?.name === "AbortError";
}

function userFacingError(error) {
  const message = error?.message || String(error);
  if (/fingerprint|workspace path|path binding/i.test(message)) {
    return "This folder no longer matches the selected Room. Use Link Existing Folder, Enter to restore, or Save as New.";
  }
  return message;
}

function createCancellationController(parentToken) {
  let cancelled = Boolean(parentToken?.isCancellationRequested);
  const listeners = new Set();
  const subscription = parentToken?.onCancellationRequested(() => controller.cancel());
  const controller = {
    token: {
      get isCancellationRequested() { return cancelled; },
      onCancellationRequested(callback) {
        if (cancelled) callback();
        else listeners.add(callback);
        return { dispose: () => listeners.delete(callback) };
      },
    },
    cancel() {
      if (cancelled) return;
      cancelled = true;
      for (const callback of [...listeners]) callback();
      listeners.clear();
    },
    dispose() {
      subscription?.dispose();
      listeners.clear();
    },
  };
  return controller;
}

function setRuntimeLifecycle(state, message) {
  runtimeLifecycle = { state, message };
  roomsProvider?.emitter?.fire(undefined);
}

async function chooseLocalFallback(error) {
  const reason = managedRuntime.localFallbackReason(error);
  if (!reason) return "unavailable";
  const reasonText = reason === "controller-artifact-unpublished"
    ? "the controller artifact is not published yet"
    : "the portable environment is incompatible with this host";
  while (true) {
    const action = await vscode.window.showWarningMessage(
      `Portable runtime is unavailable on this host because ${reasonText}. Build the verified bundled runtime locally now? This may take several minutes and requires network access.`,
      "Build Locally", "Show Logs", "Cancel",
    );
    if (action === "Show Logs") {
      outputChannel?.show(true);
      continue;
    }
    return action === "Build Locally" ? "build" : "cancelled";
  }
}

async function clearLocalFallback() {
  if (!extensionContext) throw new Error("Josh Room extension runtime is not activated");
  await managedRuntime.clearLocalFallbackRecord(extensionContext);
  managedRuntimePromise = undefined;
  managedJatPromise = undefined;
  runtimeLifecycle = { state: "UNINITIALIZED", message: "Retrying portable Josh Room runtime" };
  roomsProvider?.emitter?.fire(undefined);
  if (roomsProvider) await roomsProvider.refresh();
  return "portable-runtime-retry";
}

function controllerRootFor(context, manifest) {
  const controllerRobot = manifest.controller?.robot;
  if (typeof controllerRobot !== "string" || path.isAbsolute(controllerRobot)
    || controllerRobot.split(/[\\/]+/).includes("..")) {
    throw new Error("Josh Room runtime manifest has an unsafe controller recipe path");
  }
  const extensionRoot = path.resolve(context.extensionPath);
  const controllerPath = path.resolve(extensionRoot, controllerRobot);
  const relativeController = path.relative(extensionRoot, controllerPath);
  if (!relativeController || relativeController.startsWith("..") || path.isAbsolute(relativeController)) {
    throw new Error("Josh Room controller recipe escapes the VSIX");
  }
  return path.dirname(controllerPath);
}

function controllerSourceVersion(controllerRoot) {
  const digest = crypto.createHash("sha256");
  const files = [];
  const visit = (directory, relative = "") => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
      if (entry.name === "__pycache__" || entry.name.endsWith(".pyc")) continue;
      const child = path.join(directory, entry.name);
      const childRelative = path.posix.join(relative, entry.name);
      if (entry.isDirectory()) visit(child, childRelative);
      else if (entry.isFile()) files.push([childRelative, child]);
    }
  };
  visit(controllerRoot);
  for (const [relative, filename] of files) {
    digest.update(relative);
    digest.update(fs.readFileSync(filename));
  }
  return digest.digest("hex");
}

function fallbackIdentity(manifest, rcc, controllerRoot, jatArtifactDigest) {
  return {
    mode: "local-build-fallback",
    extension_version: manifest.extension_version,
    rcc_version: rcc.version,
    platform: managedRuntime.resolvePlatform(),
    jat_source_sha: manifest.jat.git_sha,
    ...(jatArtifactDigest ? { jat_artifact_digest: jatArtifactDigest } : {}),
    portable_jat_artifact_digest: manifest.jat.environment_artifact.digest,
    controller_source_version: controllerSourceVersion(controllerRoot),
    controller_artifact_digest: manifest.controller.environment_artifact?.digest || "unpublished",
  };
}

async function localRuntimeState(context, manifest, rcc, jat, error, progressReporter, controllerRoot) {
  progressReporter?.event({ stage: "runtime", message: `LOCAL BUILD FALLBACK: ${error.message}` });
  progressReporter?.event({ stage: "runtime", message: "Building controller environment locally; JAT remains lazy until a JAT operation" });
  const jatRoot = jat?.jatRoot || await managedRuntime.ensureJatSource(context, manifest.jat);
  const identityBase = fallbackIdentity(manifest, rcc, controllerRoot);
  const warm = await managedRuntime.verifyLocalFallback(
    context, rcc, path.join(controllerRoot, "robot.yaml"), identityBase,
  );
  const marker = managedRuntime.readLocalFallbackRecord(context);
  const localJatDigest = marker?.jat_artifact_digest || jat?.artifact;
  const identity = fallbackIdentity(manifest, rcc, controllerRoot, localJatDigest);
  let localJat;
  let readyIdentity = identity;
  if (warm) {
    progressReporter?.event({ stage: "runtime", message: "Reusing verified LOCAL BUILD FALLBACK controller environment" });
  } else {
    if (await chooseLocalFallback(error) !== "build") throw new Error("Local runtime build cancelled");
    await managedRuntime.prepareLocalController(
      context,
      rcc,
      path.join(controllerRoot, "robot.yaml"),
      {
        onProgress: (event) => progressReporter?.event({ stage: "runtime", message: event.message }),
        onOutput: (stream, chunk) => {
          if (stream !== "stderr") return;
          const sanitized = sanitizeRuntimeLine(chunk);
          if (!sanitized) return;
          outputChannel?.appendLine(`${new Date().toISOString()} RCC ${stream}: ${sanitized}`);
          progressReporter?.event({ stage: "runtime", message: sanitized });
        },
      },
    );
    if (error.fallbackReason === "environment-compatibility") {
      localJat = await managedRuntime.buildLocalJatArtifact(
        context,
        rcc,
        path.join(jatRoot, "robot.yaml"),
        {
          onProgress: (event) => progressReporter?.event({ stage: "runtime", message: event.message }),
          onOutput: (stream, chunk) => {
            if (stream !== "stderr") return;
            const sanitized = sanitizeRuntimeLine(chunk);
            if (!sanitized) return;
            outputChannel?.appendLine(`${new Date().toISOString()} RCC ${stream}: ${sanitized}`);
            progressReporter?.event({ stage: "runtime", message: sanitized });
          },
        },
      );
    }
    readyIdentity = fallbackIdentity(manifest, rcc, controllerRoot, localJat?.artifact || jat?.artifact);
    await managedRuntime.writeLocalFallbackRecord(context, { ...readyIdentity, ...(localJat ? { local_jat_artifact_digest: localJat.artifact } : {}) });
    progressReporter?.event({
      stage: "runtime",
      message: localJat
        ? "Controller and local JAT artifacts ready (LOCAL BUILD FALLBACK)"
        : "Controller environment ready (LOCAL BUILD FALLBACK); JAT remains lazy",
    });
  }
  const effectiveJatDigest = localJat?.artifact || localJatDigest || jat?.artifact;
  return {
    manifest,
    rcc,
    jat: jat ? { ...jat, artifact: effectiveJatDigest || jat.artifact } : { jatRoot, artifact: effectiveJatDigest, sourceSha: manifest.jat.git_sha },
    controller: undefined,
    controllerRoot,
    mode: "local-build-fallback",
    fallbackReason: error.fallbackReason,
    localReady: warm,
    localIdentity: readyIdentity,
  };
}

function startRuntimeReadiness(context, progressReporter) {
  if (managedRuntimePromise) return managedRuntimePromise;
  setRuntimeLifecycle("RCC_RESOLVING", "Resolving managed RCC");
  const source = runtimeReadinessOverride || initializeManagedRuntime(context, progressReporter);
  managedRuntimePromise = Promise.resolve(source).then((state) => {
    setRuntimeLifecycle("RUNTIME_READY", "Managed runtime ready; cached artifacts reused when available");
    return state || testRuntime;
  }).catch((error) => {
    setRuntimeLifecycle("FAILED", `Josh Room runtime unavailable: ${error.message}`);
    managedRuntimePromise = undefined;
    throw error;
  });
  return managedRuntimePromise;
}

async function initializeManagedRuntime(context, progressReporter) {
  const manifest = managedRuntime.readManifest();
  setStatus("$(sync~spin) Josh Room", "Preparing managed runtime…");
  progressReporter?.event({ stage: "runtime", message: "Resolving managed RCC" });
  const rcc = await managedRuntime.ensureManagedRcc(context, manifest, {
    onProgress: (event) => progressReporter?.event({ stage: "runtime", message: event.message }),
  });
  const controllerRoot = controllerRootFor(context, manifest);
  progressReporter?.event({ stage: "runtime", message: "Preparing Josh Room controller environment" });
  let controller;
  try {
    controller = await managedRuntime.ensureControllerRuntime(context, manifest, rcc, {
      onProgress: (event) => progressReporter?.event({ stage: "runtime", message: event.message }),
    });
  } catch (error) {
    if (managedRuntime.localFallbackReason(error)) return localRuntimeState(context, manifest, rcc, undefined, error, progressReporter, controllerRoot);
    throw error;
  }
  return {
    manifest,
    rcc,
    jat: undefined,
    controller,
    controllerRoot,
  };
}

function operationNeedsJat(args) {
  if (!Array.isArray(args) || !args.length) return false;
  if (["hydrate", "serve", "jat", "doctor"].includes(args[0])) return true;
  return args[0] === "snapshot" && args[1] === "create";
}

async function ensureJatForState(context, state, progressReporter) {
  if (state.mode === "local-build-fallback" || state.jat?.artifact) return state.jat;
  if (!managedJatPromise) {
    progressReporter?.event({ stage: "runtime", message: "Preparing JAT runtime for this operation" });
    managedJatPromise = managedRuntime.ensureJatRuntime(context, state.manifest, state.rcc, {
      onProgress: (event) => progressReporter?.event({ stage: "runtime", message: event.message }),
      onOutput: (stream, chunk) => {
        const sanitized = sanitizeRuntimeLine(chunk);
        if (!sanitized) return;
        outputChannel?.appendLine(`${new Date().toISOString()} RCC ${stream}: ${sanitized}`);
        progressReporter?.event({ stage: "runtime", message: sanitized });
      },
    }).then((jat) => {
      state.jat = jat;
      return jat;
    }).catch((error) => {
      managedJatPromise = undefined;
      throw error;
    });
  }
  return managedJatPromise;
}

async function runtimeFor(cwd, args = [], progressReporter) {
  if (testRuntime) {
    setRuntimeLifecycle("RUNTIME_READY", "Managed runtime ready");
    return testRuntime;
  }
  if (!extensionContext) throw new Error("Josh Room extension runtime is not activated");
  const state = await startRuntimeReadiness(extensionContext);
  if (operationNeedsJat(args)) await ensureJatForState(extensionContext, state, progressReporter);
  const localFallback = state.mode === "local-build-fallback";
  return {
    command: state.rcc.executable,
    args: localFallback
      ? (args) => ["run", "--silent", "-r", path.join(state.controllerRoot, "robot.yaml"), "-t", "Josh Room", "--", ...args, "--json"]
      : (args, receiptFile) => [
        "--no-build", "env", "exec", "--artifact", state.controller.artifact,
        "--permissive-local", "--inherit-streams", "--receipt-file", receiptFile,
        "--", "python", "-m", "josh_room", ...args, "--json",
      ],
    env: managedRuntime.runtimeEnvironment(extensionContext, {
      rccExecutable: state.rcc.executable,
      controllerRoot: state.controllerRoot,
      jatRoot: state.jat?.jatRoot,
      jatArtifact: state.jat?.artifact,
      jatSourceSha: state.jat?.sourceSha,
      controllerArtifact: state.controller?.artifact,
    }, cwd),
    jatRoot: state.jat?.jatRoot,
    jatArtifact: state.jat?.artifact,
    mode: state.mode,
    markLocalReady: state.mode === "local-build-fallback"
      ? () => managedRuntime.writeLocalFallbackRecord(extensionContext, state.localIdentity)
      : undefined,
  };
}

async function writeRuntimeCredentials(environment, { extensionMode = true } = {}) {
  environment.JOSH_ROOM_EXTENSION_MODE = extensionMode ? "1" : "0";
  if (!extensionContext?.secrets?.get) return () => {};
  const serialized = await extensionContext.secrets.get(CREDENTIALS_SECRET);
  if (!serialized) return () => {};
  let value;
  try {
    value = JSON.parse(serialized);
  } catch (error) {
    throw new Error(`stored Josh Room credentials are invalid: ${error.message}`);
  }
  if (!value || typeof value !== "object" || !value.profiles || typeof value.profiles !== "object") {
    throw new Error("stored Josh Room credentials are invalid");
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-credentials-"));
  fs.chmodSync(directory, 0o700);
  const filename = path.join(directory, "credentials.json");
  fs.writeFileSync(filename, `${JSON.stringify(value)}\n`, { mode: 0o600 });
  fs.chmodSync(filename, 0o600);
  environment.JOSH_ROOM_PROVIDER_CREDENTIALS = filename;
  return () => fs.rmSync(directory, { recursive: true, force: true });
}

async function rememberExtensionCredentials(profile, credentials) {
  if (!extensionContext?.secrets?.get || !extensionContext?.secrets?.store || !profile || !credentials) return;
  let value = { profiles: {} };
  const serialized = await extensionContext.secrets.get(CREDENTIALS_SECRET);
  if (serialized) {
    try {
      const decoded = JSON.parse(serialized);
      if (decoded && typeof decoded === "object" && decoded.profiles && typeof decoded.profiles === "object") {
        value = { profiles: { ...decoded.profiles } };
      }
    } catch (_error) {
      // Replace only malformed extension-owned storage after a successful new credential handoff.
    }
  }
  value.profiles[profile] = {
    "access-key-id": credentials["access-key-id"],
    "secret-access-key": credentials["secret-access-key"],
    ...(credentials["session-token"] ? { "session-token": credentials["session-token"] } : {}),
  };
  await extensionContext.secrets.store(CREDENTIALS_SECRET, JSON.stringify(value));
}

function encryptionSecretKey(domain, generation) {
  return `${ENCRYPTION_SECRET_PREFIX}${domain}:${generation}`;
}

function recoverySecretKey(domain) {
  return `${RECOVERY_SECRET_PREFIX}${domain}`;
}

async function cacheEncryptionMaterial(result, dimension) {
  const domain = result?.encryption_domain_id || dimension?.encryption_domain_id;
  const generation = result?.key_generation || dimension?.key_generation;
  const material = result?.encryption_material || result?.material || result?.identity;
  if (!extensionContext?.secrets?.store || !domain || !generation || !material) return;
  let identity = material;
  if (typeof identity === "string" && fs.existsSync(identity)) identity = fs.readFileSync(identity, "utf8");
  if (typeof identity !== "string" || Buffer.byteLength(identity) > 16 * 1024) {
    throw new Error("encryption material is invalid or too large");
  }
  const cached = {
    identity,
    ...(result.recipient ? { recipient: result.recipient } : {}),
    ...(Array.isArray(result.recovery_recipients) ? { recovery_recipients: result.recovery_recipients } : {}),
  };
  await extensionContext.secrets.store(encryptionSecretKey(domain, generation), JSON.stringify(cached));
  if (result.recovery_material) {
    await extensionContext.secrets.store(recoverySecretKey(domain), String(result.recovery_material));
  }
}

async function readEncryptionMaterial(dimension) {
  const domain = dimension?.encryption_domain_id || dimension?.encryptionDomainId;
  const generation = dimension?.key_generation || dimension?.keyGeneration;
  if (!domain || !generation || !extensionContext?.secrets?.get) return undefined;
  const value = await extensionContext.secrets.get(encryptionSecretKey(domain, generation));
  if (!value) return undefined;
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed.identity === "string") return parsed;
  } catch (_error) {
    // Older extension-owned values may contain the identity directly.
  }
  return { identity: value };
}

async function writeEncryptionHandoff(environment, material) {
  delete environment.JOSH_ROOM_ENCRYPTION_MATERIAL;
  delete environment.JOSH_ROOM_IDENTITY;
  delete environment.JOSH_ROOM_SELECTED_RECIPIENTS;
  delete environment.JOSH_ROOM_SELECTED_DOMAIN;
  if (!material) return () => {};
  let identity = typeof material === "string" ? material : material.identity;
  if (typeof identity === "string" && fs.existsSync(identity)) identity = fs.readFileSync(identity, "utf8");
  if (typeof identity !== "string" || !identity.trim() || Buffer.byteLength(identity) > 16 * 1024) {
    throw new Error("selected encryption material is invalid or too large");
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-encryption-"));
  fs.chmodSync(directory, 0o700);
  const filename = path.join(directory, "material.identity");
  try {
    fs.writeFileSync(filename, identity.endsWith("\n") ? identity : `${identity}\n`, { mode: 0o600 });
    fs.chmodSync(filename, 0o600);
  } catch (error) {
    fs.rmSync(directory, { recursive: true, force: true });
    throw error;
  }
  environment.JOSH_ROOM_ENCRYPTION_MATERIAL = filename;
  environment.JOSH_ROOM_IDENTITY = filename;
  if (material.domain || material.encryption_domain_id) {
    environment.JOSH_ROOM_SELECTED_DOMAIN = material.domain || material.encryption_domain_id;
  }
  if (material.recipients || material.recipient) {
    environment.JOSH_ROOM_SELECTED_RECIPIENTS = (material.recipients || [material.recipient]).join(",");
  }
  return () => fs.rmSync(directory, { recursive: true, force: true });
}

function parseControllerOutput(output) {
  try {
    return JSON.parse(output);
  } catch (_error) {
    const start = output.indexOf("{");
    const end = output.lastIndexOf("}");
    if (start < 0 || end < start) throw new Error("Josh Room controller returned no JSON result");
    return JSON.parse(output.slice(start, end + 1));
  }
}

function sanitizeRuntimeLine(line) {
  let value = String(line).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").trim();
  value = value.replace(/\bAGE-SECRET-KEY-[A-Za-z0-9_-]+/g, "[REDACTED AGE IDENTITY]");
  value = value.replace(/\bage1[0-9a-z]{20,}/g, "[REDACTED AGE RECIPIENT]");
  value = value.replace(/(bearer\s+)[^\s]+/ig, "$1[REDACTED]");
  value = value.replace(/((?:access[-_ ]?key|secret[-_ ]?key|session[-_ ]?token|token|password|oauth[-_ ]?code|authorization|auth|identity|credential|credentials|secret|key|stdin|argv|env)\s*[:=]\s*)[^\s,]+/ig, "$1[REDACTED]");
  value = value.replace(/https?:\/\/[^\s]+/ig, (match) => {
    try {
      const url = new URL(match.replace(/[),.;]+$/, ""));
      return `${url.origin}${url.pathname}${url.search || url.hash ? "/[REDACTED]" : ""}`;
    } catch (_error) {
      return "[REDACTED URL]";
    }
  });
  return value.slice(0, 240);
}

function sanitizeProgressEvent(event) {
  if (!event || typeof event !== "object") return event;
  return { ...event, message: sanitizeRuntimeLine(event.message) };
}

const CONTROLLER_DIAGNOSTIC_LIMIT = 4096;

function sanitizeControllerText(value) {
  return String(value || "")
    .replace(/\0/g, "")
    .split(/\r?\n/)
    .map((line) => sanitizeRuntimeLine(line))
    .filter(Boolean)
    .join(" ")
    .slice(-CONTROLLER_DIAGNOSTIC_LIMIT);
}

function sanitizeControllerArgv(argv) {
  const sanitized = [];
  const sensitiveFlag = /^-{1,2}(?:access[-_ ]?key|secret[-_ ]?key|session[-_ ]?token|token|password|oauth[-_ ]?code|authorization|auth|identity|credential|credentials|secret|key|bearer)(?:$|[-_=])/i;
  const sensitiveAssignment = /^(?:[a-z_][a-z0-9_]*(?:access[-_ ]?key|secret[-_ ]?key|session[-_ ]?token|token|password|oauth[-_ ]?code|authorization|auth|identity|credential|credentials|secret|key)|access[-_ ]?key|secret[-_ ]?key|session[-_ ]?token|token|password|oauth[-_ ]?code|authorization|auth|identity|credential|credentials|secret|key)\s*=/i;
  for (let index = 0; index < argv.length; index += 1) {
    const value = String(argv[index]);
    if (sensitiveFlag.test(value) && !value.includes("=")) {
      sanitized.push(sanitizeRuntimeLine(value));
      if (index + 1 < argv.length) sanitized.push("[REDACTED]");
      index += 1;
    } else if (sensitiveAssignment.test(value)) {
      sanitized.push(sanitizeRuntimeLine(value));
    } else {
      sanitized.push(sanitizeRuntimeLine(value));
    }
  }
  return sanitized;
}

function collectResultDiagnostics(value, key, diagnostics, inDiagnostic = false) {
  const diagnosticContext = inDiagnostic || /diagnostic/i.test(String(key || ""));
  if (typeof value === "string") {
    if (diagnosticContext) diagnostics.push(sanitizeControllerText(value));
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((entry) => collectResultDiagnostics(entry, key, diagnostics, diagnosticContext));
    return;
  }
  if (value && typeof value === "object") {
    Object.entries(value).forEach(([entryKey, entry]) => collectResultDiagnostics(entry, entryKey, diagnostics, diagnosticContext));
  }
}

function resultDiagnostic(result) {
  const diagnostics = [];
  collectResultDiagnostics(result, "", diagnostics);
  return [...new Set(diagnostics.filter(Boolean))].join(" ").slice(-CONTROLLER_DIAGNOSTIC_LIMIT);
}

function redactResultDiagnostics(value, key = "", inDiagnostic = false) {
  if (/^argv$/i.test(key) && Array.isArray(value)) return sanitizeControllerArgv(value);
  const diagnosticContext = inDiagnostic || /error|message|diagnostic|stdout|stderr/i.test(key);
  if (typeof value === "string") return diagnosticContext ? sanitizeControllerText(value) : value;
  if (Array.isArray(value)) return value.map((entry) => redactResultDiagnostics(entry, key, diagnosticContext));
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).map(([entryKey, entry]) => [
    entryKey,
    redactResultDiagnostics(entry, entryKey, diagnosticContext),
  ]));
}

function controllerFailure(result, { controllerExitStatus, controllerStderr } = {}) {
  const diagnostic = resultDiagnostic(result);
  const safeResult = redactResultDiagnostics(result);
  const jat = result?.jat && typeof result.jat === "object" ? result.jat : {};
  const jatExitStatus = jat.exit_status ?? jat.exitStatus ?? result?.exit_status;
  const safeStderr = sanitizeControllerText(controllerStderr);
  const baseMessage = sanitizeControllerText(result?.error) || "Josh Room operation failed.";
  let message = String(baseMessage);
  if (diagnostic && message.includes(diagnostic)) {
    const parts = message.split(diagnostic);
    message = `${parts[0]}${diagnostic}${parts.slice(1).join("")}`.trim();
  }
  if (diagnostic && !message.includes(diagnostic)) {
    const available = CONTROLLER_DIAGNOSTIC_LIMIT - diagnostic.length - 2;
    message = diagnostic.length + 2 >= CONTROLLER_DIAGNOSTIC_LIMIT
      ? diagnostic
      : `${message.slice(0, Math.max(0, available))}: ${diagnostic}`;
  }
  const failure = new Error(message);
  failure.result = {
    ...safeResult,
    ...(controllerExitStatus !== undefined ? { controller_exit_status: controllerExitStatus } : {}),
    ...(jatExitStatus !== undefined ? { jat_exit_status: jatExitStatus } : {}),
    ...(safeStderr ? { controller_stderr: safeStderr } : {}),
    ...(diagnostic ? { diagnostic } : {}),
  };
  if (controllerExitStatus !== undefined) failure.controller_exit_status = controllerExitStatus;
  if (jatExitStatus !== undefined) failure.jat_exit_status = jatExitStatus;
  if (safeStderr) failure.controller_stderr = safeStderr;
  if (diagnostic) failure.diagnostic = diagnostic;
  return failure;
}

async function executeJoshRoom(args, cwd, cancellationToken, progressReporter, stdinPayload, options = {}) {
  const progressDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-progress-"));
  fs.chmodSync(progressDirectory, 0o700);
  const progressPath = path.join(progressDirectory, "events.jsonl");
  const resultPath = path.join(progressDirectory, "result.json");
  const receiptPath = path.join(progressDirectory, "rcc-receipt.json");
  fs.writeFileSync(progressPath, "", { mode: 0o600 });
  let runtime;
  let environment;
  let credentialsCleanup = () => {};
  let encryptionCleanup = () => {};
  try {
    runtime = await runtimeFor(cwd, args, progressReporter);
    environment = { ...process.env, ...(runtime.env || {}), JOSH_ROOM_PROGRESS_FILE: progressPath };
    credentialsCleanup = await writeRuntimeCredentials(environment, {
      extensionMode: runtime.mode !== "local-build-fallback" || Boolean(runtime.jatArtifact),
    });
    encryptionCleanup = await writeEncryptionHandoff(environment, options.encryptionMaterial);
  } catch (error) {
    encryptionCleanup();
    credentialsCleanup();
    fs.rmSync(progressDirectory, { recursive: true, force: true });
    throw error;
  }
  return new Promise((resolve, reject) => {
    environment.JOSH_ROOM_RESULT_FILE = resultPath;
    const followers = [followProgressFile(progressPath, (event) => progressReporter?.event(sanitizeProgressEvent(event)))];
    if (["snapshot", "hydrate", "serve", "jat"].includes(args[0])) {
      const jatRoot = runtime.jatRoot || environment.JOSH_ROOM_JAT_ROOT;
      const streamJat = (line, severity = "info") => {
        const safeLine = sanitizeRuntimeLine(line);
        outputChannel?.[severity](safeLine);
        const stage = stageForLog(safeLine);
        if (stage) progressReporter?.event({ stage: "jat", message: stage });
      };
      followers.push(
        followLogFile(path.join(jatRoot, "output", "stdout.log"), (line) => streamJat(line)),
        followLogFile(path.join(jatRoot, "output", "stderr.log"), (line) => streamJat(line, "warn")),
      );
    }
    let cleaned = false;
    const cleanup = () => {
      if (cleaned) return;
      cleaned = true;
      if (cleanup.timer) clearTimeout(cleanup.timer);
      for (const follower of followers) follower.dispose();
      fs.rmSync(progressDirectory, { recursive: true, force: true });
      credentialsCleanup();
      encryptionCleanup();
    };
    let child;
    try {
      child = childProcess.spawn(runtime.command, runtime.args(args, receiptPath), {
        cwd,
        env: environment,
        detached: true,
        stdio: [stdinPayload === undefined ? "ignore" : "pipe", "pipe", "pipe"],
      });
    } catch (error) {
      cleanup();
      reject(error);
      return;
    }
    progressReporter?.event({ stage: "controller", message: "Starting Josh Room controller" });
    if (stdinPayload !== undefined) child.stdin.end(stdinPayload);
    let stdout = "";
    let stderr = "";
    let cancelled = Boolean(cancellationToken?.isCancellationRequested);
    const append = (current, chunk) => (current + chunk.toString()).slice(-1024 * 1024);
    const streamBuffers = { stdout: "", stderr: "" };
    const stream = (name, chunk) => {
      if (name !== "stderr") return;
      const current = streamBuffers[name] + chunk.toString();
      const lines = current.split(/\r?\n/);
      streamBuffers[name] = lines.pop() || "";
      for (const line of lines) {
        const sanitized = sanitizeRuntimeLine(line);
        if (!sanitized) continue;
        outputChannel?.appendLine(`${new Date().toISOString()} RCC ${name}: ${sanitized}`);
        progressReporter?.event({ stage: "controller", message: sanitized });
      }
    };
    child.stdout.on("data", (chunk) => { stdout = append(stdout, chunk); stream("stdout", chunk); });
    child.stderr.on("data", (chunk) => { stderr = append(stderr, chunk); stream("stderr", chunk); });
    const cancellation = cancellationToken?.onCancellationRequested(() => {
      cancelled = true;
      cleanup();
      terminateChild(child);
    });
    child.on("error", (error) => {
      cancellation?.dispose();
      cleanup();
      reject(cancelled ? cancellationError() : error);
    });
    child.on("close", (code) => {
      cancellation?.dispose();
      let result;
      let receipt;
      try {
        if (fs.existsSync(receiptPath)) receipt = parseControllerOutput(fs.readFileSync(receiptPath, "utf8"));
      } catch (error) {
        outputChannel?.warn(`Unable to read RCC receipt: ${error.message}`);
      }
      const receiptExit = receipt && (receipt.exitCode ?? receipt.exit_code ?? receipt.exit);
      if (receiptExit !== undefined && Number(receiptExit) !== 0) {
        const detail = sanitizeControllerText(
          receipt.compatibility || receipt.error || receipt.message || `RCC controller exited with status ${receiptExit}`,
        );
        cleanup();
        const failure = new Error(String(detail));
        failure.receipt_exit_status = Number(receiptExit);
        failure.stdout = sanitizeControllerText(stdout);
        failure.stderr = sanitizeControllerText(stderr);
        reject(failure);
        return;
      }
      try {
        if (fs.existsSync(resultPath)) result = parseControllerOutput(fs.readFileSync(resultPath, "utf8"));
      } catch (error) {
        outputChannel?.warn(`Unable to read controller result receipt: ${error.message}`);
      }
      cleanup();
      if (cancelled || cancellationToken?.isCancellationRequested) {
        reject(cancellationError());
        return;
      }
      if (result) {
        resolve({ stdout: JSON.stringify(result), stderr, runtime, controllerExitStatus: code });
        return;
      }
      if (code === 0) {
        resolve({ stdout, stderr, runtime });
      } else {
        const error = new Error(`Josh Room exited with status ${code}`);
        try {
          error.result = redactResultDiagnostics(parseControllerOutput(stdout));
        } catch (_parseError) {
          // Non-JSON failures use the sanitized bounded stream fields below.
        }
        error.stdout = sanitizeControllerText(stdout);
        error.stderr = sanitizeControllerText(stderr);
        error.controller_exit_status = code;
        error.command = runtime.command;
        error.args = runtime.args(args);
        error.resultPath = resultPath;
        reject(error);
      }
    });
    if (options.timeoutMs > 0) {
      cleanup.timer = setTimeout(() => {
        cancelled = true;
        cleanup();
        terminateChild(child);
      }, options.timeoutMs);
      cleanup.timer.unref?.();
    }
  });
}

async function runJoshRoom(args, cwd, cancellationToken, progressReporter, stdinPayload, options = {}) {
  outputChannel?.info(`START · controller ${args.join(" ")}`);
  try {
    const execution = await executeJoshRoom(args, cwd, cancellationToken, progressReporter, stdinPayload, options);
    const { stdout, runtime } = execution;
    const result = parseControllerOutput(stdout);
    // `status` deliberately returns ok=false for a changed or unlinked
    // workspace. That is authoritative state, not an operation failure.
    if (!result.ok && args[0] !== "status") {
      throw controllerFailure(result, {
        controllerExitStatus: execution.controllerExitStatus,
        controllerStderr: execution.stderr,
      });
    }
    if (runtime?.markLocalReady) await runtime.markLocalReady();
    outputChannel?.info(`DONE · ${result.project_id || result.operation || args[0]}`);
    return result;
  } catch (error) {
    if (cancellationToken?.isCancellationRequested || isCancellationError(error)) {
      throw cancellationError();
    }
    if (error.result) {
      if (args[0] === "status") {
        outputChannel?.info(`DONE · status (${error.result.state || "unknown"})`);
        return error.result;
      }
      throw controllerFailure(error.result, {
        controllerExitStatus: error.controller_exit_status,
        controllerStderr: error.stderr,
      });
    }
    const output = error.stdout || error.stderr;
    if (output) {
      let result;
      try {
        result = parseControllerOutput(output);
      } catch (parseError) {
        outputChannel?.error(sanitizeControllerText(error.message || String(error)));
        outputChannel?.show(true);
        throw error;
      }
      if (args[0] === "status") {
        outputChannel?.info(`DONE · status (${result.state || "unknown"})`);
        return result;
      }
      throw controllerFailure(result, {
        controllerExitStatus: error.controller_exit_status,
        controllerStderr: error.stderr,
      });
    }
    outputChannel?.error(error.message || String(error));
    outputChannel?.show(true);
    throw error;
  }
}

function runOperation(title, args, cwd, { cancellable = true, stdin, encryptionMaterial, dimension, timeoutMs } = {}) {
  const operationId = ++activeOperationId;
  const displayTitle = title.replace(/…$/, "");
  return vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title, cancellable },
    async (progress, token) => {
      const reporter = createVisualReporter(displayTitle, operationKind(args), progress, operationId);
      try {
        const result = await runJoshRoom(args, cwd, token, reporter, stdin, {
          encryptionMaterial: encryptionMaterial || (dimension && await readEncryptionMaterial(dimension)),
          timeoutMs,
        });
        reporter.finish();
        return result;
      } catch (error) {
        reporter.fail(error);
        throw error;
      }
    },
  );
}

function createVisualReporter(title, kind, progress, operationId = ++activeOperationId) {
  const tracker = createProgressTracker(kind);
  let lastPercent = 0;
  let latestState;
  let frame = 0;
  const startedAt = Date.now();
  let animation;
  const updateStatus = () => {
    if (!latestState) return;
    const animated = formatProgressDisplay(title, kind, latestState, frame, Date.now() - startedAt);
    frame += 1;
    const tooltip = new vscode.MarkdownString();
    tooltip.appendCodeblock(animated.tooltip);
    tooltip.appendMarkdown("\nFull details: **Output → Josh Room**");
    setStatus(animated.statusText, tooltip);
  };
  const stopAnimation = () => {
    if (animation) clearInterval(animation);
    animation = undefined;
  };
  const publish = (event) => {
    event = sanitizeProgressEvent(event);
    const state = tracker.update(event);
    latestState = state;
    const display = formatProgressDisplay(title, kind, state, 0, Date.now() - startedAt);
    const increment = state.percent === undefined ? undefined : Math.max(0, state.percent - lastPercent);
    if (state.percent !== undefined) lastPercent = state.percent;
    progress.report({ message: display.notification, ...(increment ? { increment } : {}) });
    updateStatus();
    if (state.percent === 100) stopAnimation();
    else if (!animation) {
      animation = setInterval(updateStatus, 140);
      animation.unref?.();
    }
    outputChannel?.info(display.logLine);
  };
  return {
    event: publish,
    finish: () => {
      publish({ stage: "complete", message: "Complete" });
      stopAnimation();
      if (activeOperationId === operationId) {
        setStatus(`$(pass-filled) ${title} 100%`, "Completed successfully");
        setTimeout(() => {
          if (activeOperationId === operationId) refreshRoomStatus();
        }, 2500);
      }
    },
    fail: (error) => {
      stopAnimation();
      const rawMessage = error?.message || String(error);
      const message = userFacingError(error);
      outputChannel?.error(`FAILED · ${title} · ${rawMessage}`);
      if (activeOperationId === operationId) setStatus(`$(error) ${title} failed`, message);
    },
    dispose: stopAnimation,
  };
}

class RoomsProvider {
  constructor() {
    this.rooms = undefined;
    this.state = "initial";
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  getTreeItem(item) {
    if (item.kind !== "room") {
      const labels = {
        load: "Load Rooms",
        loading: "Loading Rooms…",
        empty: "No saved Rooms",
        error: "Couldn't load Rooms — click to retry",
      };
      const treeItem = new vscode.TreeItem(labels[item.kind], vscode.TreeItemCollapsibleState.None);
      treeItem.iconPath = new vscode.ThemeIcon(
        item.kind === "loading" ? "sync~spin" : item.kind === "error" ? "error" : "cloud-download",
      );
      treeItem.command = { command: "joshRoom.refresh", title: "Load Rooms" };
      return treeItem;
    }
    const current = sameRoomBinding(currentRoom(activeWorkspace()), item);
    const saved = current && !roomDirty && workspaceBindingTrusted;
    const treeItem = new vscode.TreeItem(item.display_name, vscode.TreeItemCollapsibleState.None);
    treeItem.description = current ? saved ? "Current • Saved" : "Current • Needs save" : "";
    treeItem.tooltip = current
      ? saved ? `${item.display_name} is saved` : `${item.display_name} has workspace changes to save`
      : item.display_name;
    treeItem.contextValue = "room";
    treeItem.iconPath = new vscode.ThemeIcon(current ? roomDirty ? "circle-filled" : "home" : "archive");
    if (!current) {
      treeItem.command = { command: "joshRoom.enter", title: "Enter Room", arguments: [item] };
    }
    return treeItem;
  }

  getChildren() {
    if (this.state === "loading") return [{ kind: "loading" }];
    if (this.state === "error") return [{ kind: "error" }];
    if (this.state === "initial") return [{ kind: "load" }];
    if (!this.rooms?.length) return [];
    return this.rooms.map((room) => ({ ...room, kind: "room" }));
  }

  async refresh() {
    this.state = "loading";
    setStatus("$(sync~spin) Josh Room", "Loading Rooms…");
    this.emitter.fire(undefined);
    try {
      const catalog = await loadCatalog(activeWorkspace(), "Refreshing Rooms…");
      this.rooms = catalog.projects;
      this.state = "ready";
      await vscode.commands.executeCommand("setContext", "joshRoom.roomsEmpty", this.rooms.length === 0);
      setStatus(`$(archive) ${this.rooms.length} Room${this.rooms.length === 1 ? "" : "s"}`);
      refreshRoomStatus();
      this.emitter.fire(undefined);
    } catch (error) {
      this.state = "error";
      await vscode.commands.executeCommand("setContext", "joshRoom.roomsEmpty", false);
      setStatus("$(error) Josh Room", "Couldn't load Rooms — click to retry");
      this.emitter.fire(undefined);
      throw error;
    }
  }
}

class JatToolsProvider {
  getTreeItem(item) {
    const treeItem = new vscode.TreeItem(item.label, vscode.TreeItemCollapsibleState.None);
    treeItem.description = item.description;
    treeItem.iconPath = new vscode.ThemeIcon(item.icon);
    treeItem.command = { command: item.command, title: item.label };
    return treeItem;
  }

  getChildren() {
    return [
      { label: "Pack Folder into JAT", description: "Build", icon: "package", command: "joshRoom.jatBuild" },
      { label: "Inspect JAT", description: "Inventory", icon: "search", command: "joshRoom.jatInspect" },
      { label: "Extract from JAT", description: "One reference", icon: "go-to-file", command: "joshRoom.jatExtract" },
      { label: "Restore Workspace", description: "Restore", icon: "folder-library", command: "joshRoom.jatRestore" },
      { label: "Serve JAT…", description: "Auto · Files · Registry · Both", icon: "server-process", command: "joshRoom.jatServe" },
      { label: "Export Images…", description: "containerd", icon: "archive", command: "joshRoom.jatExport" },
      { label: "Copy / Seed…", description: "registry · dir", icon: "copy", command: "joshRoom.jatCopy" },
    ];
  }
}

async function loadCatalog(cwd, title = "Loading your Rooms…") {
  return runOperation(title, ["projects", "list", "--backend", "r2"], cwd);
}

function currentRoom(cwd) {
  try {
    const marker = JSON.parse(fs.readFileSync(path.join(cwd, ".josh-room.json"), "utf8"));
    return isRoomMarker(marker) ? marker : undefined;
  } catch (_error) {
    return undefined;
  }
}

function roomBindingDimensionId(item) {
  const project = item && item.project && typeof item.project === "object" ? item.project : item;
  const dimension = (item && item.dimension) || (project && project.dimension);
  return (dimension && (dimension.id || dimension.dimension_id))
    || (project && (project.dimension_id || project.dimensionId))
    || (item && item.dimension_id);
}

function sameRoomBinding(marker, item) {
  const project = roomProject(item);
  const snapshotId = snapshotIdentity(item);
  return Boolean(marker && project
    && marker.project_id === project.id
    && marker.dimension_id === roomBindingDimensionId(item)
    && (!snapshotId || marker.snapshot_id === snapshotId));
}

function refreshRoomStatus() {
  let marker;
  try {
    marker = currentRoom(activeWorkspace());
  } catch (_error) {
    marker = undefined;
  }
  if (!marker) {
    statusItem.command = "workbench.view.extension.josh-room";
    setStatus("$(archive) Josh Room");
    return;
  }
  const saved = !roomDirty && workspaceBindingTrusted;
  if (!saved) {
    statusItem.command = "joshRoom.save";
    setStatus(
      `$(circle-filled) ${marker.display_name} — Save`,
      workspaceBindingTrusted
        ? "Workspace changed. Click to save this Room."
        : "Workspace binding is not trusted. Link or Repair this Room before trusting it.",
    );
  } else {
    statusItem.command = "workbench.view.extension.josh-room";
    setStatus(`$(check) ${marker.display_name} — Saved`, "This Room matches its last saved workspace state.");
  }
}

function setRoomDirty(dirty) {
  if (roomDirty === dirty) return;
  roomDirty = dirty;
  roomsProvider?.emitter.fire(undefined);
  refreshRoomStatus();
}

function relativeWorkspacePath(uri) {
  let root;
  try {
    root = activeWorkspace();
  } catch (_error) {
    return undefined;
  }
  const relative = path.relative(root, uri.fsPath);
  return shouldMarkDirty(relative) ? relative : undefined;
}

async function markWorkspaceChange(uri) {
  const relative = relativeWorkspacePath(uri);
  if (!relative || !workspaceBaseline) return;
  try {
    const changed = await workspaceBaseline.check(relative);
    setRoomDirty(changed || dirtyBuffers.size > 0);
  } catch (error) {
    outputChannel?.warn(`Unable to compare ${relative} with the saved Room: ${error.message}`);
    setRoomDirty(true);
  }
}

async function startDirtyTracking(context) {
  const generation = ++dirtyTrackingGeneration;
  workspaceWatcher?.dispose();
  workspaceWatcher = undefined;
  workspaceBaseline = undefined;
  dirtyBuffers.clear();
  let root;
  try {
    root = activeWorkspace();
  } catch (_error) {
    return;
  }
  if (!currentRoom(root)) {
    setRoomDirty(false);
    return;
  }
  workspaceWatcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(root, "**/*"));
  const compare = (uri) => markWorkspaceChange(uri);
  workspaceWatcher.onDidChange(compare);
  workspaceWatcher.onDidCreate(compare);
  workspaceWatcher.onDidDelete(compare);
  context.subscriptions.push(workspaceWatcher);
  workspaceBaseline = new WorkspaceBaseline(root);
  setStatus("$(sync~spin) Indexing saved Room", "Preparing exact change detection…");
  await workspaceBaseline.capture();
  if (generation !== dirtyTrackingGeneration) return;
  setRoomDirty(false);
  refreshRoomStatus();
}

async function saveRoom(options = {}) {
  assertWorkspaceTrusted("save a Room");
  const cwd = activeWorkspace();
  const folders = await vscode.window.showOpenDialog({
    title: "Josh: Save Room",
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: "Save this folder",
  });
  if (!folders?.length) return "cancelled";
  const source = folders[0].fsPath;
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  const projects = nativeRegistry.flattenDimensionRooms(catalog);
  const marker = currentRoom(source);
  const selected = options.forceCreate
    ? { create: true }
    : await vscode.window.showQuickPick([
      { label: "$(add) Create a new Room…", create: true },
      ...projects.map((project) => ({
        label: roomLabel(project),
        description: sameRoomBinding(marker, project) ? "Current Room" : "Save a new latest snapshot",
        project,
      })),
    ], { title: "Josh: Save Room", placeHolder: "Create or update a Room", ignoreFocusOut: true });
  if (!selected) return "cancelled";
  let name = selected.project?.display_name;
  let targetDimension;
  if (selected.create) {
    name = await vscode.window.showInputBox({
      title: "Josh: Save Room",
      prompt: "Name this Room",
      ignoreFocusOut: true,
      validateInput: (value) => value.trim() ? undefined : "Enter a Room name.",
    });
  }
  if (!name) return "cancelled";
  let targetDimensionId = dimensionId(selected.project);
  if (selected.create) {
    targetDimension = await chooseDimension(catalog, "Where should this Room be saved?");
    if (!targetDimension) return "cancelled";
    targetDimensionId = dimensionId(targetDimension);
  } else if (selected.project?.dimension) {
    targetDimension = selected.project.dimension;
  }
  if (targetDimensionId) selectedDimensionId = targetDimensionId;
  if (selected.project) {
    const existing = path.join(path.dirname(cwd), selected.project.id);
    if (existing !== source && sameRoomBinding(currentRoom(existing), selected.project)) {
      const action = await vscode.window.showInformationMessage(
        `“${selected.project.display_name}” already has a working folder.`,
        "Open Room",
      );
      if (action === "Open Room") {
        await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(existing), false);
      }
      return "opened";
    }
    if (!sameRoomBinding(marker, selected.project)) {
      const confirmed = await vscode.window.showWarningMessage(
        `Replace the latest “${selected.project.display_name}” snapshot with the contents of ${source}? The previous snapshot remains recoverable.`,
        { modal: true },
        "Replace Latest",
      );
      if (confirmed !== "Replace Latest") return "cancelled";
    }
  }
  const targetProvider = nativeRegistry.providerKey(
    targetDimension?.provider || selected.project?.provider || selected.project?.dimension?.provider,
  );
  if (targetProvider === "r2"
    && targetDimension
    && catalog.auth_state
    && catalog.auth_state !== "connected") {
    const connected = await connectCloudflare({ dimension: targetDimension });
    if (connected !== "connected") return connected;
  }
  if (targetProvider === "minio") {
    const authStatus = await runJoshRoom(
      ["encryption", "status", "--dimension", targetDimensionId], cwd, undefined, undefined,
    );
    const encryptionReady = authStatus.state === "ready"
      || (authStatus.state === undefined && authStatus.encryption_state === undefined);
    if (!encryptionReady) {
      const connected = await connectEncryption({
        dimension: targetDimension || { id: targetDimensionId, provider: targetProvider },
      });
      if (connected !== "initialized") return connected;
    }
  }
  const imageChoice = await vscode.window.showQuickPick([
    { label: "Workspace only", allImages: false },
    { label: "Workspace + all tagged local OCI images", allImages: true },
  ], { title: "Include local OCI images?", ignoreFocusOut: true });
  if (!imageChoice) return "cancelled";
  const buildArgs = nativeRegistry.dimensionArgs(
    ["snapshot", "create", name, "--source", source, "--backend", targetProvider],
    targetDimensionId || selectedDimensionId,
  );
  if (imageChoice.allImages) buildArgs.push("--all-images");
  const result = targetProvider === "minio"
    ? await runSelectedEncryption(buildArgs, source, targetDimension || { id: targetDimensionId, provider: targetProvider }, {
      title: `Saving ${name}…`,
      action: "save a Room",
    })
    : await runOperation(`Saving ${name}…`, buildArgs, source);
  const size = (result.ciphertext_size / (1024 * 1024)).toFixed(1);
  await vscode.window.showInformationMessage(`Saved “${name}” (${size} MiB).`);
  if (path.resolve(source) === path.resolve(cwd)) {
    await startDirtyTracking(extensionContext);
  }
  await roomsProvider?.refresh();
  return "saved";
}

async function enterRoom(preferredProject) {
  assertWorkspaceTrusted("enter a Room");
  const cwd = activeWorkspace();
  const preferred = roomProject(preferredProject);
  const preferredSnapshotId = snapshotIdentity(preferredProject);
  const selected = preferred
    ? { label: roomLabel(preferred), projectId: preferred.id, project: preferred, snapshotId: preferredSnapshotId }
    : await (async () => {
      const catalog = await loadCatalog(cwd);
      const projects = nativeRegistry.flattenDimensionRooms(catalog);
      return vscode.window.showQuickPick(
        projects.map((project) => ({ label: roomLabel(project), projectId: project.id, project })),
        { title: "Josh: Enter Room", placeHolder: "What do you want to work on?", ignoreFocusOut: true },
      );
    })();
  if (!selected) return "cancelled";
  const selectedDimensionId = dimensionId(selected.project);
  const provider = nativeRegistry.providerKey(selected.project.provider || selected.project.dimension?.provider);
  if (!selected.projectId || !selectedDimensionId || !["local", "r2", "minio"].includes(provider)) {
    throw new Error("Choose a trusted Provider, Dimension, Room, and JAT recovery point.");
  }
  if (!/^[a-z0-9-]+$/.test(selected.projectId) || !/^[a-z0-9][a-z0-9._-]*$/.test(selectedDimensionId)) {
    throw new Error("Provider, Dimension, or Room identity is unsafe for workspace materialization.");
  }
  selected.dimensionId = selectedDimensionId;
  const existing = findMaterialization(cwd, selectedDimensionId, selected.projectId);
  if (existing) {
    rememberMaterialization(existing);
    if (!selected.snapshotId || existing.marker.snapshot_id === selected.snapshotId) {
      if (path.resolve(existing.path) === path.resolve(cwd)) {
        await vscode.window.showInformationMessage(`“${selected.label}” is already open.`);
        return "current";
      }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Opening existing ${selected.label}…`, cancellable: false },
        () => vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(existing.path), false),
      );
      return "opened";
    }
    const switched = await switchMaterialization(cwd, selected, existing, provider);
    if (switched !== "switched") return switched;
    await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(existing.path), false);
    return "opened";
  }
  let history;
  let snapshotId = selected.snapshotId || "latest";
  if (!selected.snapshotId) {
    const historyArgs = nativeRegistry.dimensionArgs(
      ["snapshots", "list", selected.projectId, "--backend", provider], selectedDimensionId,
    );
    history = provider === "minio"
      ? await runSelectedEncryption(historyArgs, cwd, selected.project?.dimension || { id: selectedDimensionId, provider }, {
        title: `Loading ${selected.label} recovery points…`, action: "read a Room",
      })
      : await runOperation(`Loading ${selected.label} recovery points…`, historyArgs, cwd);
    snapshotId = "latest";
    if (history.snapshots.length > 1) {
      const snapshot = await vscode.window.showQuickPick(
        history.snapshots
          .map((item) => ({
            label: item.snapshot_id === history.latest ? "Latest snapshot" : "Previous snapshot",
            description: item.created_at || item.snapshot_id,
            snapshotId: item.snapshot_id,
          }))
          .sort((left, right) => left.snapshotId === history.latest ? -1 : right.snapshotId === history.latest ? 1 : 0),
        { title: `Josh: Enter ${selected.label}`, placeHolder: "Choose a recovery point", ignoreFocusOut: true },
      );
      if (!snapshot) return "cancelled";
      snapshotId = snapshot.snapshotId;
    }
  }
  const destination = newMaterializationDestination(cwd, selectedDimensionId, selected.projectId);
  if (fs.existsSync(destination)) {
    throw new Error(`Refusing to replace an unexplained existing folder: ${destination}`);
  }
  const restoreArgs = nativeRegistry.dimensionArgs([
      "hydrate",
      selected.projectId,
      "--snapshot",
      snapshotId,
      "--destination",
      destination,
      "--backend",
      provider,
      "--ide",
      "terminal",
    ], selectedDimensionId);
  if (provider === "minio") {
    await runSelectedEncryption(restoreArgs, cwd, selected.project?.dimension || { id: selectedDimensionId, provider }, {
      title: `Restoring ${selected.label}…`, action: "restore a Room",
    });
  } else {
    await runOperation(`Restoring ${selected.label}…`, restoreArgs, cwd);
  }
  const materialization = corroboratedMaterialization(destination, selectedDimensionId, selected.projectId);
  if (materialization) rememberMaterialization(materialization);
  await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false);
  return "opened";
}

async function removeRoom(preferredProject) {
  return removeSnapshot(preferredProject);
}

async function removeSnapshot(preferredProject) {
  const cwd = activeWorkspace();
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  const projects = nativeRegistry.flattenDimensionRooms(catalog);
  const preferred = roomProject(preferredProject);
  const selectedRoom = preferred
    ? { label: roomLabel(preferred), project: preferred }
    : await vscode.window.showQuickPick(
      projects.map((project) => ({ label: roomLabel(project), project })),
      { title: "Josh: Delete Snapshot", placeHolder: "Choose a Room", ignoreFocusOut: true },
    );
  if (!selectedRoom) return "cancelled";
  const preferredSnapshotId = snapshotIdentity(preferredProject);
  const marker = currentRoom(cwd);
  if (preferredSnapshotId && activeSnapshot(marker, selectedRoom.project, preferredSnapshotId)) {
    await vscode.window.showWarningMessage(
      "Cannot delete the active JAT while this workspace is linked to it. Enter another recovery point first.",
    );
    return "blocked";
  }
  const history = await runOperation(
    `Loading ${selectedRoom.label} recovery points…`,
    nativeRegistry.dimensionArgs(["snapshots", "list", selectedRoom.project.id, "--backend", "r2"], dimensionId(selectedRoom.project)),
    cwd,
  );
  const selectedSnapshot = preferredSnapshotId
    ? { label: "Selected snapshot", snapshot: { snapshot_id: preferredSnapshotId } }
    : await vscode.window.showQuickPick(
      history.snapshots
      .map((snapshot) => ({
        label: snapshot.snapshot_id === history.latest ? "$(star-full) Latest snapshot" : "$(history) Previous snapshot",
        description: snapshot.created_at ? new Date(snapshot.created_at).toLocaleString() : "Date unavailable",
        detail: snapshot.snapshot_id,
        snapshot,
      }))
      .sort((left) => left.snapshot.snapshot_id === history.latest ? -1 : 1),
      {
        title: `Delete a ${selectedRoom.label} snapshot`,
        placeHolder: "Choose exactly one snapshot",
        ignoreFocusOut: true,
      },
    );
  if (!selectedSnapshot) return "cancelled";
  if (activeSnapshot(marker, selectedRoom.project, selectedSnapshot.snapshot.snapshot_id)) {
    await vscode.window.showWarningMessage(
      "Cannot delete the active JAT while this workspace is linked to it. Enter another recovery point first.",
    );
    return "blocked";
  }
  const consequence = history.snapshots.length === 1
    ? " This is the final snapshot and also deletes the Room."
    : selectedSnapshot.snapshot.snapshot_id === history.latest
    ? " The newest remaining snapshot will become Latest."
    : "";
  const confirmed = await vscode.window.showWarningMessage(
    `Permanently delete the ${selectedSnapshot.label.replace(/^\$\([^)]*\) /, "").toLowerCase()} from ${selectedRoom.label}?${consequence}`,
    { modal: true },
    "Delete Snapshot",
  );
  if (confirmed !== "Delete Snapshot") return "cancelled";
  const result = await runOperation(
    `Removing ${selectedRoom.label} recovery point…`,
    nativeRegistry.dimensionArgs(["snapshots", "remove", selectedRoom.project.id, selectedSnapshot.snapshot.snapshot_id, "--backend", "r2"], dimensionId(selectedRoom.project)),
    cwd,
  );
  await vscode.window.showInformationMessage(
    result.room_removed
      ? `Final snapshot deleted. “${selectedRoom.label}” is gone.`
      : result.latest_promoted
        ? `Snapshot deleted. Latest is now ${result.latest}.`
        : "Snapshot deleted.",
  );
  await roomsProvider?.refresh();
  return "removed";
}

async function serveRoom(preferredProject, { startRegistry = startRegistryTerminal } = {}) {
  assertWorkspaceTrusted("serve a Room");
  const cwd = activeWorkspace();
  const marker = currentRoom(cwd);
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  const projects = nativeRegistry.flattenDimensionRooms(catalog);
  const project = await chooseServeProject(projects, marker, preferredProject);
  if (!project) return "cancelled";
  const dimension = dimensionId(project);
  const provider = nativeRegistry.providerKey(project.provider || project.dimension?.provider);
  if (!project.id || !dimension || !["local", "r2", "minio"].includes(provider)) {
    throw new Error("Choose a trusted Provider, Dimension, Room, and JAT recovery point.");
  }
  const history = await runOperation(
    `Loading ${project.display_name} recovery points…`,
    nativeRegistry.dimensionArgs(["snapshots", "list", project.id], dimension),
    cwd,
  );
  const preferredSnapshotId = snapshotIdentity(preferredProject);
  const markerSnapshotId = marker && marker.format_version === 2 && marker.snapshot_id;
  if (preferredSnapshotId && !history.snapshots.some((item) => item.snapshot_id === preferredSnapshotId)) {
    throw new Error("The selected JAT is not available in the selected Room.");
  }
  let snapshotId = preferredSnapshotId || (markerSnapshotId && history.snapshots.some(
    (item) => item.snapshot_id === markerSnapshotId,
  ) ? markerSnapshotId : history.latest);
  if (!preferredSnapshotId && snapshotId === history.latest && history.snapshots.length > 1 && snapshotId !== markerSnapshotId) {
    const selected = await vscode.window.showQuickPick(
      history.snapshots.map((item) => ({
        label: item.snapshot_id === history.latest ? "Latest snapshot" : "Previous snapshot",
        description: item.created_at || item.snapshot_id,
        snapshotId: item.snapshot_id,
      })),
      { title: `Serve images from ${project.display_name}`, placeHolder: "Choose a recovery point", ignoreFocusOut: true },
    );
    if (!selected) return "cancelled";
    snapshotId = selected.snapshotId;
  }
  if (!/^[a-z0-9-]+$/.test(project.id) || !/^[a-z0-9-]+$/.test(snapshotId)
    || !/^[a-z0-9][a-z0-9._-]*$/.test(dimension)) {
    throw new Error("Provider, Dimension, Room, or snapshot identity is unsafe for terminal execution.");
  }
  let encryptionMaterial;
  if (provider === "minio") {
    const status = await runJoshRoom(["encryption", "status", "--dimension", dimension], cwd);
    encryptionMaterial = await readEncryptionMaterial({ ...(project.dimension || {}), ...status });
  }
  return startRegistry({
    cwd,
    title: `Serving ${project.display_name}`,
    terminalName: `Images: ${project.display_name}`,
    args: ["serve", project.id, "--snapshot", snapshotId, "--backend", provider, "--dimension", dimension],
    encryptionMaterial,
    retry: () => vscode.commands.executeCommand("joshRoom.serve", project),
  });
}

function shellQuote(value, platform = process.platform) {
  const text = String(value);
  if (platform === "win32") return '"' + text.replaceAll('"', '\\"') + '"';
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

function shellJoin(values, platform = process.platform) {
  return values.map((value) => shellQuote(value, platform)).join(" ");
}

function buildTerminalLaunch(runtime, args, environment, platform = process.platform, receiptFile) {
  return {
    command: shellJoin([runtime.command, ...runtime.args(args, receiptFile)], platform),
    environment: { ...environment },
  };
}

function isSafeRestoreName(value) {
  const name = String(value ?? "").trim();
  return Boolean(
    name
      && name !== "."
      && name !== ".."
      && !name.includes("\0")
      && !/[\\/]/.test(name)
      && !path.posix.isAbsolute(name)
      && !path.win32.isAbsolute(name)
      && !/^[A-Za-z]:/.test(name),
  );
}

function terminateChild(child, platform = process.platform) {
  if (platform === "win32") {
    child.kill("SIGTERM");
    return;
  }
  try {
    process.kill(-child.pid, "SIGTERM");
  } catch (_error) {
    child.kill("SIGTERM");
  }
}

function formatHaulerSize(size) {
  if (typeof size !== "number" || !Number.isFinite(size) || size < 0) return undefined;
  if (size < 1024) return `${size} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = size;
  let unit = "B";
  for (const candidate of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = candidate;
  }
  return `${value >= 100 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

function resultPayloads(result, fallbackPath) {
  if (Array.isArray(result?.payloads) && result.payloads.length) return result.payloads;
  if (result?.payload_path) return [{ path: result.payload_path, size: result.payload_size, sha256: result.sha256 }];
  return fallbackPath ? [{ path: fallbackPath }] : [];
}

function payloadReceipt(payloads) {
  return payloads
    .map((output) => {
      const size = formatHaulerSize(output.size);
      const digest = typeof output.sha256 === "string" && output.sha256.length >= 16 ? ` sha256 ${output.sha256.slice(0, 16)}…` : "";
      return `${output.path}${size ? ` (${size})` : ""}${digest}`;
    })
    .join(", ");
}

function isHaulerChunkSize(value) {
  return /^[1-9][0-9]*(?:[KMGT](?:B)?)?$/i.test(String(value || "").trim());
}

function isSafeCopyTarget(value, scheme) {
  const target = String(value || "").trim();
  if (!target.startsWith(scheme) || /\s/.test(target)) return false;
  const remainder = target.slice(scheme.length);
  return Boolean(remainder) && !remainder.includes("@") && !remainder.includes("?") && !remainder.includes("#");
}

async function chooseHaul(cwd, title, openLabel = "Use this haul") {
  const files = await vscode.window.showOpenDialog({
    title,
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: true,
    canSelectFolders: false,
    canSelectMany: false,
    filters: { "JAT Hauler archive": ["zst", "tar"] },
    openLabel,
  });
  return files?.length ? files[0].fsPath : undefined;
}

function inventoryQuickPickItems(inventory) {
  return (Array.isArray(inventory) ? inventory : [])
    .filter((entry) => entry && typeof entry.reference === "string" && entry.reference)
    .map((entry) => ({
      label: `$(package) ${entry.reference}`,
      description: [entry.type, entry.platform, formatHaulerSize(entry.size)].filter(Boolean).join(" · "),
      entry,
    }));
}

async function jatBuild() {
  const cwd = activeWorkspace();
  const folders = await vscode.window.showOpenDialog({
    title: "JAT: Pack Folder into JAT",
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: "Pack this folder",
  });
  if (!folders?.length) return "cancelled";
  const source = folders[0].fsPath;
  const output = await vscode.window.showSaveDialog({
    title: "Save portable JAT haul",
    defaultUri: vscode.Uri.file(path.join(path.dirname(source), `${path.basename(source)}.haul.tar.zst`)),
    filters: { "JAT Hauler archive": ["zst"] },
  });
  if (!output) return "cancelled";
  const imageChoice = await vscode.window.showQuickPick([
    { label: "Workspace only", allImages: false },
    { label: "Workspace + all tagged local OCI images", allImages: true },
  ], { title: "Include local OCI images?", ignoreFocusOut: true });
  if (!imageChoice) return "cancelled";
  const advanced = await vscode.window.showQuickPick([
    { label: "images.txt image list (Hauler-native sync)", id: "imagesFile" },
    { label: "Hauler manifest (advanced declarative composition)", id: "manifests" },
    { label: "Chunked haul (split into sized chunks)", id: "chunk" },
    { label: "Slim build (exclude signatures, attestations, SBOMs)", id: "slim" },
  ], {
    title: "Advanced capture inputs",
    placeHolder: "Optional — press Esc to keep the simple one-click pack",
    canPickMany: true,
    ignoreFocusOut: true,
  });
  const advancedIds = new Set((Array.isArray(advanced) ? advanced : []).map((item) => item.id));
  const args = ["jat", "build", "--source", source, "--output", output.fsPath];
  if (imageChoice.allImages) args.push("--all-images");
  if (advancedIds.has("imagesFile")) {
    const lists = await vscode.window.showOpenDialog({
      title: "Choose images.txt consumed by Hauler",
      defaultUri: vscode.Uri.file(source),
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: true,
      filters: { "Image list": ["txt"] },
      openLabel: "Use this image list",
    });
    if (!lists) return "cancelled";
    for (const list of lists) args.push("--images-file", list.fsPath);
  }
  if (advancedIds.has("manifests")) {
    const manifests = await vscode.window.showOpenDialog({
      title: "Choose Hauler manifests (Files / Images / Charts)",
      defaultUri: vscode.Uri.file(source),
      canSelectFiles: true,
      canSelectFolders: false,
      canSelectMany: true,
      filters: { "Hauler manifest": ["yaml", "yml"] },
      openLabel: "Use these manifests",
    });
    if (!manifests) return "cancelled";
    for (const manifest of manifests) args.push("--hauler-manifest", manifest.fsPath);
  }
  if (advancedIds.has("chunk")) {
    const chunkSize = await vscode.window.showInputBox({
      title: "Chunked haul size",
      prompt: "Split the haul into chunks of this size (e.g. 500M, 1G)",
      placeHolder: "500M",
      ignoreFocusOut: true,
      validateInput: (value) => isHaulerChunkSize(value) ? undefined : "Use a positive byte count with an optional K/KM/M/MB/G/GB/T/TB unit, e.g. 500M.",
    });
    if (!chunkSize) return "cancelled";
    args.push("--chunk-size", chunkSize.trim());
  }
  if (advancedIds.has("slim")) {
    const slimChoice = await vscode.window.showWarningMessage(
      "Slim build excludes cosign signatures, attestations, SBOMs, and other referrer extras from remote acquisition. Keep full supply-chain extras by default?",
      { modal: true },
      "Use slim build",
      "Keep full extras",
    );
    if (!slimChoice) return "cancelled";
    if (slimChoice === "Use slim build") args.push("--exclude-extras");
  }
  const result = await runOperation(`Packing ${path.basename(source)}…`, args, cwd);
  const payloads = resultPayloads(result, output.fsPath);
  const action = await vscode.window.showInformationMessage(
    payloads.length > 1
      ? `Created ${payloads.length} chunk files starting at ${payloads[0].path}.`
      : `Created ${payloads[0]?.path || output.fsPath}.`,
    "Show Logs",
  );
  if (action === "Show Logs") outputChannel?.show(true);
  return "built";
}

async function jatInspect() {
  const cwd = activeWorkspace();
  const haul = await chooseHaul(cwd, "JAT: Inspect Haul");
  if (!haul) return "cancelled";
  return showJatInventory(haul, cwd);
}

async function showJatInventory(haul, cwd) {
  const result = await runOperation(`Inspecting ${path.basename(haul)}…`, ["jat", "inspect", "--haul", haul], cwd);
  const anchors = result.anchors && typeof result.anchors === "object"
    ? Object.entries(result.anchors).filter(([, present]) => present).map(([kind]) => kind)
    : [];
  const items = inventoryQuickPickItems(result.inventory);
  if (!items.length) {
    await vscode.window.showInformationMessage(
      `JAT inspect found no content references in ${path.basename(haul)}.`,
      "Show Logs",
    );
    return "empty";
  }
  const selected = await vscode.window.showQuickPick(items, {
    title: `JAT inventory · ${items.length} ${items.length === 1 ? "reference" : "references"}`,
    placeHolder: `JAT anchors: ${anchors.length ? anchors.join(", ") : "none"} — select a reference for details`,
    ignoreFocusOut: true,
  });
  if (!selected) return "inspected";
  const detail = [
    selected.entry.type || "content",
    selected.entry.platform,
    formatHaulerSize(selected.entry.size),
    selected.entry.digest,
  ].filter(Boolean).join(" · ");
  const action = await vscode.window.showInformationMessage(
    `${selected.entry.reference}${detail ? ` — ${detail}` : ""}`,
    "Copy Reference",
    "Extract…",
  );
  if (action === "Copy Reference") {
    await vscode.env.clipboard.writeText(selected.entry.reference);
    await vscode.window.showInformationMessage(`Copied ${selected.entry.reference}.`);
  } else if (action === "Extract…") {
    return jatExtract({ haul, reference: selected.entry.reference });
  }
  return "inspected";
}

async function jatExtract(preselection) {
  const cwd = activeWorkspace();
  const haul = preselection?.haul || await chooseHaul(cwd, "JAT: Extract from JAT");
  if (!haul) return "cancelled";
  let reference = typeof preselection?.reference === "string" ? preselection.reference : undefined;
  if (!reference) {
    const inspected = await runOperation(`Inspecting ${path.basename(haul)}…`, ["jat", "inspect", "--haul", haul], cwd);
    const items = inventoryQuickPickItems(inspected.inventory);
    if (!items.length) {
      await vscode.window.showInformationMessage(`JAT inspect found no extractable references in ${path.basename(haul)}.`);
      return "empty";
    }
    const selected = await vscode.window.showQuickPick(items, {
      title: "Extract which reference?",
      placeHolder: "Content comes from the JAT inspect inventory — nothing is restored to a workspace",
      ignoreFocusOut: true,
    });
    if (!selected) return "cancelled";
    reference = selected.entry.reference;
  }
  const parents = await vscode.window.showOpenDialog({
    title: "Choose extract parent folder",
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: "Extract here",
  });
  if (!parents?.length) return "cancelled";
  const defaultName = `${path.basename(haul).replace(/\.tar\.zst$|\.zst$|\.tar$/i, "")}-extract`;
  const name = await vscode.window.showInputBox({
    title: "JAT: Extract from JAT",
    prompt: "New destination folder name (created only if absent)",
    value: defaultName,
    ignoreFocusOut: true,
    validateInput: (value) => isSafeRestoreName(value) ? undefined : "Enter one folder name without path separators.",
  });
  if (!name) return "cancelled";
  const destination = path.join(parents[0].fsPath, name);
  if (fs.existsSync(destination)) throw new Error(`Destination already exists: ${destination}`);
  const result = await runOperation(
    `Extracting ${reference}…`,
    ["jat", "extract", "--haul", haul, "--reference", reference, "--destination", destination],
    cwd,
  );
  const payloads = resultPayloads(result, destination);
  await vscode.window.showInformationMessage(
    `Extracted ${reference} to ${payloadReceipt(payloads) || destination}.`,
    "Show Logs",
  );
  return "extracted";
}

async function jatRestore() {
  const cwd = activeWorkspace();
  const files = await vscode.window.showOpenDialog({
    title: "JAT: Restore Haul",
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: true,
    canSelectFolders: false,
    canSelectMany: false,
    filters: { "JAT Hauler archive": ["zst"] },
    openLabel: "Restore this haul",
  });
  if (!files?.length) return "cancelled";
  const haul = files[0].fsPath;
  const parents = await vscode.window.showOpenDialog({
    title: "Choose restore parent folder",
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: "Restore here",
  });
  if (!parents?.length) return "cancelled";
  const defaultName = path.basename(haul).replace(/\.tar\.zst$|\.zst$/i, "") + "-restored";
  const name = await vscode.window.showInputBox({
    title: "JAT: Restore Haul",
    prompt: "New destination folder name",
    value: defaultName,
    ignoreFocusOut: true,
    validateInput: (value) => isSafeRestoreName(value) ? undefined : "Enter one folder name without path separators.",
  });
  if (!name) return "cancelled";
  const destination = path.join(parents[0].fsPath, name);
  if (fs.existsSync(destination)) throw new Error(`Destination already exists: ${destination}`);
  await runOperation(
    `Restoring ${path.basename(haul)}…`,
    ["jat", "restore", "--haul", haul, "--destination", destination],
    cwd,
  );
  const action = await vscode.window.showInformationMessage(`Restored to ${destination}.`, "Open Folder");
  if (action === "Open Folder") {
    await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false);
  }
  return "restored";
}

const JAT_SERVE_MODES = [
  { label: "Auto", description: "Let JAT choose the compatible projection", mode: "auto" },
  { label: "Files", description: "Direct downloads / file artifacts", mode: "files" },
  { label: "Registry", description: "OCI images and charts", mode: "registry" },
  { label: "Files + Registry", description: "Expose both from the same capsule", mode: "both" },
];

async function jatServe() {
  const cwd = activeWorkspace();
  const haul = await chooseHaul(cwd, "JAT: Serve Haul", "Serve this haul");
  if (!haul) return "cancelled";
  const chosen = await vscode.window.showQuickPick(JAT_SERVE_MODES, {
    title: "JAT: Serve Haul",
    placeHolder: "How should JAT expose this capsule?",
    ignoreFocusOut: true,
  });
  if (!chosen) return "cancelled";
  return startRegistryTerminal({
    cwd,
    title: `Serving ${path.basename(haul)}`,
    terminalName: "JAT Hauler Serve",
    args: ["jat", "serve", "--haul", haul, "--mode", chosen.mode],
    mode: chosen.mode,
    retry: () => vscode.commands.executeCommand("joshRoom.jatServe"),
  });
}

async function jatExport() {
  const cwd = activeWorkspace();
  const haul = await chooseHaul(cwd, "JAT: Export Images…");
  if (!haul) return "cancelled";
  const base = path.basename(haul).replace(/\.tar\.zst$|\.zst$|\.tar$/i, "");
  const output = await vscode.window.showSaveDialog({
    title: "Save containerd image archive",
    defaultUri: vscode.Uri.file(path.join(path.dirname(haul), `${base}-images.tar`)),
    filters: { "containerd archive": ["tar"] },
  });
  if (!output) return "cancelled";
  const result = await runOperation(
    `Exporting images from ${path.basename(haul)}…`,
    ["jat", "export", "--haul", haul, "--output", output.fsPath],
    cwd,
  );
  const payloads = resultPayloads(result, output.fsPath);
  const action = await vscode.window.showInformationMessage(
    `Exported containerd archive — ${payloadReceipt(payloads)}.`,
    "Show Logs",
  );
  if (action === "Show Logs") outputChannel?.show(true);
  return "exported";
}

async function jatCopy() {
  const cwd = activeWorkspace();
  const haul = await chooseHaul(cwd, "JAT: Copy / Seed…");
  if (!haul) return "cancelled";
  const kind = await vscode.window.showQuickPick([
    { label: "Registry", description: "Seed a remote registry (registry://…) — retry-capable transfer", scheme: "registry://" },
    { label: "Directory", description: "Project into a local directory (dir://…)", scheme: "dir://" },
  ], {
    title: "JAT: Copy / Seed",
    placeHolder: "Choose a destination type supported by JAT — credentials are never stored by Josh Room",
    ignoreFocusOut: true,
  });
  if (!kind) return "cancelled";
  let target;
  if (kind.scheme === "registry://") {
    target = await vscode.window.showInputBox({
      title: "JAT: Copy / Seed",
      prompt: "Registry target (authenticate with hauler login or a credential helper — never in this target)",
      placeHolder: "registry://registry.example.test:5000",
      ignoreFocusOut: true,
      validateInput: (value) => isSafeCopyTarget(value, "registry://") ? undefined : "Use registry://host[:port][/path] without credentials, spaces, query, or fragment.",
    });
  } else {
    const folders = await vscode.window.showOpenDialog({
      title: "Choose seed destination directory",
      defaultUri: vscode.Uri.file(cwd),
      canSelectFiles: false,
      canSelectFolders: true,
      canSelectMany: false,
      openLabel: "Seed this directory",
    });
    target = folders?.length ? `dir://${folders[0].fsPath}` : undefined;
  }
  if (!target) return "cancelled";
  const result = await runOperation(
    `Seeding ${kind.label.toLowerCase()} from ${path.basename(haul)}…`,
    ["jat", "copy", "--haul", haul, "--to", target],
    cwd,
  );
  const transfer = result.transfer && typeof result.transfer === "object" ? result.transfer : {};
  await vscode.window.showInformationMessage(
    `Seeded ${transfer.destination || target}${transfer.transport ? ` (${transfer.transport})` : ""}.`,
    "Show Logs",
  );
  return "copied";
}

function firstEndpointSuccess(attempts) {
  return new Promise((resolve, reject) => {
    let pending = attempts.length;
    let lastError;
    if (!pending) {
      resolve(undefined);
      return;
    }
    for (const attempt of attempts) {
      attempt.then(resolve, (error) => {
        lastError = error;
        pending -= 1;
        if (pending === 0) reject(lastError);
      });
    }
  });
}

function waitForServeEndpoint() {
  // Auto delegates the projection decision to JAT, so wait for the first
  // endpoint JAT actually starts (registry or fileserver) and report that.
  return firstEndpointSuccess([
    waitForRegistry().then((catalog) => ({ kind: "registry", catalog })),
    waitForFileserver().then(() => ({ kind: "files" })),
  ]);
}

async function startRegistryTerminal({ cwd, title, terminalName, args, mode = "auto", retry, encryptionMaterial }) {
  const runtime = await runtimeFor(cwd, args);
  const environment = { ...runtime.env };
  const credentialsCleanup = await writeRuntimeCredentials(environment);
  let encryptionCleanup = () => {};
  try {
    encryptionCleanup = await writeEncryptionHandoff(environment, encryptionMaterial);
  } catch (error) {
    credentialsCleanup();
    throw error;
  }
  const jatRoot = runtime.jatRoot || environment.JOSH_ROOM_JAT_ROOT;
  const progressDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-registry-"));
  fs.chmodSync(progressDirectory, 0o700);
  const progressPath = path.join(progressDirectory, "events.jsonl");
  const receiptPath = path.join(progressDirectory, "rcc-receipt.json");
  fs.writeFileSync(progressPath, "", { mode: 0o600 });
  environment.JOSH_ROOM_PROGRESS_FILE = progressPath;
  const launch = buildTerminalLaunch(runtime, args, environment, process.platform, receiptPath);
  let terminal;
  try {
    terminal = vscode.window.createTerminal({ name: terminalName, cwd, env: launch.environment });
  } catch (error) {
    credentialsCleanup();
    encryptionCleanup();
    fs.rmSync(progressDirectory, { recursive: true, force: true });
    throw error;
  }
  const followers = [];
  let latestLog = "";
  let progressReporter;
  let progressActive = false;
  const stream = (line, severity = "info") => {
    const safeLine = sanitizeRuntimeLine(line);
    latestLog = safeLine;
    outputChannel?.[severity](safeLine);
    const stage = stageForLog(safeLine);
    if (stage && progressActive) progressReporter?.event({ stage: "jat", message: stage });
  };
  followers.push(
    followProgressFile(progressPath, (event) => {
      const safeEvent = sanitizeProgressEvent(event);
      latestLog = safeEvent.message;
      if (progressActive) progressReporter?.event(safeEvent);
    }),
    followLogFile(path.join(jatRoot, "output", "stdout.log"), (line) => stream(line)),
    followLogFile(path.join(jatRoot, "output", "stderr.log"), (line) => stream(line, "warn")),
  );
  let stopped = false;
  const stopFollowing = () => {
    if (stopped) return;
    stopped = true;
    for (const follower of followers) follower.dispose();
    progressReporter?.dispose();
    credentialsCleanup();
    encryptionCleanup();
    fs.rmSync(progressDirectory, { recursive: true, force: true });
  };
  const terminalClosed = vscode.window.onDidCloseTerminal((closed) => {
    if (closed !== terminal) return;
    stopFollowing();
    terminalClosed.dispose();
    outputChannel?.info("REGISTRY · Stopped");
    refreshRoomStatus();
  });
  outputChannel?.info(`START · ${title}`);
  outputChannel?.info(`LOGS · ${path.join(jatRoot, "output")}`);
  setStatus("$(sync~spin) Starting serve endpoints", title);
  const autoServe = mode === "auto";
  const waitsForRegistry = mode !== "files";
  terminal.show(true);
  try {
    const outcome = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title,
        cancellable: false,
      },
      async (progress) => {
        progressReporter = createVisualReporter(title, "serve", progress);
        progressActive = true;
        progressReporter.event({ stage: "auth", message: "Preparing secure Room session" });
        terminal.sendText(launch.command, true);
        try {
          if (autoServe) {
            // JAT resolves Auto itself: a files-only capsule starts only the
            // fileserver. Wait for whichever endpoint JAT actually starts
            // instead of assuming the registry projection.
            return await waitForServeEndpoint();
          }
          return waitsForRegistry ? { kind: "registry", catalog: await waitForRegistry() } : undefined;
        } finally {
          progressActive = false;
        }
      },
    );
    if (!outcome || outcome.kind === "files") {
      setStatus("$(server-process) Serving files", title);
      outputChannel?.info(`READY · JAT fileserver · ${title}`);
      const filesAction = await vscode.window.showInformationMessage(
        outcome
          ? `JAT resolved this capsule to a files projection — the JAT fileserver default is ${FILESERVER_URL}. Close the ${terminalName} terminal to stop.`
          : `JAT is serving files from this capsule — the JAT fileserver default is ${FILESERVER_URL}. Close the ${terminalName} terminal to stop.`,
        "Show Logs",
      );
      if (filesAction === "Show Logs") outputChannel?.show(true);
      return "started";
    }
    const catalog = outcome.catalog;
    const repositories = Array.isArray(catalog.repositories) ? catalog.repositories : [];
    const count = repositories.length;
    const servingFiles = mode === "both";
    progressReporter?.event({ stage: "complete", message: "Serve endpoints ready" });
    setStatus("$(server-process) Registry :5000", `${count} repositories · ${title}`);
    outputChannel?.info(`READY · ${REGISTRY_URL} · ${count} ${count === 1 ? "repository" : "repositories"}`);
    const action = await vscode.window.showInformationMessage(
      servingFiles
        ? `JAT serve is ready — ${count} ${count === 1 ? "repository" : "repositories"} on ${REGISTRY_URL} and files at ${FILESERVER_URL}.`
        : `Hauler registry is ready — ${count} ${count === 1 ? "repository" : "repositories"} on ${REGISTRY_URL}.`,
      "Show Images",
      "Copy Registry URL",
      "Show Logs",
    );
    if (action === "Show Images") {
      if (!repositories.length) {
        await vscode.window.showInformationMessage("This haul contains no registry repositories.");
      } else {
        await vscode.window.showQuickPick(
          repositories.map((repository) => ({
            label: `$(package) ${repository}`,
            description: `127.0.0.1:5000/${repository}`,
          })),
          { title: "Images served by JAT", placeHolder: "Registry contents" },
        );
      }
    } else if (action === "Copy Registry URL") {
      await vscode.env.clipboard.writeText(REGISTRY_URL);
      await vscode.window.showInformationMessage(`Copied ${REGISTRY_URL}.`);
    } else if (action === "Show Logs") {
      outputChannel?.show(true);
    }
    return "started";
  } catch (error) {
    stopFollowing();
    terminal.dispose();
    terminalClosed.dispose();
    progressActive = false;
    const detail = sanitizeRuntimeLine(latestLog || error.message || String(error));
    progressReporter?.fail(error);
    outputChannel?.error(`JAT serve failed: ${detail}`);
    setStatus("$(error) JAT serve failed", detail);
    const action = await vscode.window.showErrorMessage(
      `JAT serve failed to start: ${detail.slice(0, 240)}`,
      "Show Logs",
      "Retry",
    );
    if (action === "Show Logs") outputChannel?.show(true);
    if (action === "Retry") {
      terminal.dispose();
      await retry();
    }
    return "failed";
  }
}

function register(context, command, operation) {
  context.subscriptions.push(vscode.commands.registerCommand(command, async (...args) => {
    try {
      return await operation(...args);
    } catch (error) {
      await vscode.window.showErrorMessage(error.message || String(error));
      return "failed";
    }
  }));
}

function activate(context) {
  extensionContext = context;
  outputChannel = vscode.window.createOutputChannel("Josh Room", { log: true });
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusItem.command = "workbench.view.extension.josh-room";
  setStatus("$(archive) Josh Room");
  statusItem.show();
  roomsProvider = new RoomsProvider();
  const roomsView = vscode.window.createTreeView("joshRoom.rooms", { treeDataProvider: roomsProvider });
  const jatToolsView = vscode.window.createTreeView("joshRoom.jatTools", { treeDataProvider: new JatToolsProvider() });
  context.subscriptions.push(outputChannel, statusItem, roomsView, jatToolsView);
  if (context.secrets?.onDidChange) {
    context.subscriptions.push(context.secrets.onDidChange((event) => {
      if (!event?.key || event.key.startsWith(ENCRYPTION_SECRET_PREFIX) || event.key.startsWith(RECOVERY_SECRET_PREFIX)) {
        roomsProvider.roots = undefined;
        roomsProvider.emitter.fire(undefined);
        refreshRoomStatus();
      }
    }));
  }
  context.subscriptions.push(vscode.workspace.onDidChangeTextDocument((event) => {
    const relative = relativeWorkspacePath(event.document.uri);
    if (!relative || !workspaceBaseline) return;
    if (event.document.isDirty) dirtyBuffers.add(relative);
    else dirtyBuffers.delete(relative);
    setRoomDirty(workspaceBaseline.dirty.size > 0 || dirtyBuffers.size > 0);
  }));
  context.subscriptions.push(vscode.workspace.onDidSaveTextDocument((document) => {
    const relative = relativeWorkspacePath(document.uri);
    if (!relative) return;
    dirtyBuffers.delete(relative);
    markWorkspaceChange(document.uri);
  }));
  context.subscriptions.push(vscode.workspace.onDidCloseTextDocument((document) => {
    const relative = relativeWorkspacePath(document.uri);
    if (!relative) return;
    dirtyBuffers.delete(relative);
    markWorkspaceChange(document.uri);
  }));
  register(context, "joshRoom.save", saveRoom);
  register(context, "joshRoom.new", () => saveRoom({ forceCreate: true }));
  register(context, "joshRoom.enter", enterRoom);
  register(context, "joshRoom.remove", removeRoom);
  register(context, "joshRoom.serve", serveRoom);
  register(context, "joshRoom.refresh", () => roomsProvider.refresh());
  register(context, "joshRoom.showLogs", () => outputChannel?.show(true));
  register(context, "joshRoom.clearLocalFallback", clearLocalFallback);
  register(context, "joshRoom.jatBuild", jatBuild);
  register(context, "joshRoom.jatInspect", jatInspect);
  register(context, "joshRoom.jatExtract", jatExtract);
  register(context, "joshRoom.jatRestore", jatRestore);
  register(context, "joshRoom.jatServe", jatServe);
  register(context, "joshRoom.jatExport", jatExport);
  register(context, "joshRoom.jatCopy", jatCopy);
  startDirtyTracking(context).catch((error) => {
    outputChannel.warn(`Unable to index saved Room: ${error.message}`);
    setRoomDirty(true);
  });
  roomsProvider.refresh().catch((error) => {
    outputChannel.appendLine(`error: ${error.message || String(error)}`);
  });
}

module.exports = { activate };
module.exports.__test__ = {
  enterRoom,
  resetNativeBaseline,
  saveRoom,
  setSelectedDimensionId(value) {
    selectedDimensionId = value;
  },
  setStatusItem(value) {
    statusItem = value;
  },
  setOutputChannelForTests(value) {
    outputChannel = value;
  },
  startDirtyTracking,
};

const nativeRegistry = require("./registry");
let selectedDimensionId;
let lastKnownCatalog;

function dimensionList(catalog) {
  if (Array.isArray(catalog && catalog.dimensions)) return catalog.dimensions;
  if (catalog && catalog.dimensions && typeof catalog.dimensions === "object") {
    return Object.entries(catalog.dimensions).map(([id, value]) =>
      Object.assign({}, value || {}, { id: (value && value.id) || id }));
  }
  return [];
}

function dimensionId(item) {
  return (item && item.dimension && (item.dimension.id || item.dimension.dimension_id))
    || (item && (item.dimension_id || item.id))
    || selectedDimensionId;
}

function explicitDimensionId(item) {
  const project = item && item.project && typeof item.project === "object" ? item.project : item;
  const dimension = (item && item.dimension) || (project && project.dimension);
  return (dimension && (dimension.id || dimension.dimension_id))
    || (project && (project.dimension_id || project.dimensionId))
    || (item && item.dimension_id);
}

function snapshotIdentity(item) {
  const snapshot = item && item.snapshot;
  return (item && (item.snapshot_id || item.snapshotId))
    || (snapshot && (snapshot.snapshot_id || snapshot.id));
}

function activeSnapshot(marker, project, snapshotId) {
  return Boolean(marker && project && snapshotId
    && marker.project_id === project.id
    && marker.dimension_id === explicitDimensionId(project)
    && marker.snapshot_id === snapshotId);
}

function roomProject(item) {
  const project = item && item.project && typeof item.project === "object" ? item.project : item;
  if (!project) return undefined;
  const dimension = (item && item.dimension) || project.dimension;
  const id = project.id || project.project_id || (item && item.id);
  return {
    ...project,
    id,
    project_id: project.project_id || id,
    dimension_id: project.dimension_id || (dimension && (dimension.id || dimension.dimension_id)),
    dimension,
  };
}

function roomLabel(item) {
  const project = roomProject(item);
  if (!project) return undefined;
  const name = project.display_name || project.name || project.id;
  const dimension = project.dimension || (item && item.dimension);
  const dimensionName = project.dimension_display_name
    || (dimension && (dimension.display_name || dimension.name || dimension.id || dimension.dimension_id))
    || project.dimension_id;
  return dimensionName ? `${name} · ${dimensionName}` : name;
}

async function chooseServeProject(projects, marker, preferredProject) {
  const preferred = roomProject(preferredProject);
  if (preferred && explicitDimensionId(preferredProject)) {
    const selected = projects.find((project) => project.id === preferred.id
      && explicitDimensionId(project) === explicitDimensionId(preferredProject));
    if (selected) return selected;
  }
  if (marker && marker.format_version === 2 && marker.dimension_id && marker.snapshot_id) {
    const selected = projects.find((project) => project.id === marker.project_id
      && explicitDimensionId(project) === marker.dimension_id
      && Array.isArray(project.snapshots)
      && project.snapshots.some((snapshot) => snapshot.snapshot_id === marker.snapshot_id));
    if (selected) return selected;
  }
  const choice = await vscode.window.showQuickPick(
    projects.map((project) => ({ label: roomLabel(project), project })),
    { title: "Josh: Serve Room Images", placeHolder: "Choose a Room", ignoreFocusOut: true },
  );
  return choice && choice.project;
}

async function chooseDimension(catalog, title) {
  const dimensions = dimensionList(catalog);
  if (!dimensions.length) throw new Error("No storage is configured. Add Storage first.");
  const preferred = selectedDimensionId
    && dimensions.find((item) => (item.id || item.dimension_id) === selectedDimensionId);
  if (preferred || dimensions.length === 1) {
    const selected = preferred || dimensions[0];
    selectedDimensionId = selected.id || selected.dimension_id;
    return selected;
  }
  const choice = await vscode.window.showQuickPick(
    dimensions.map((dimension) => ({
      label: dimension.display_name || dimension.name || dimension.id || dimension.dimension_id,
      description: nativeRegistry.providerLabel(dimension.provider),
      dimension,
    })),
    { title: title || "Where should this Room be saved?", placeHolder: "Choose a storage destination", ignoreFocusOut: true },
  );
  if (!choice) return undefined;
  selectedDimensionId = choice.dimension.id || choice.dimension.dimension_id;
  return choice.dimension;
}

async function connectCloudflare(item, { timeoutMs = 600000, purpose } = {}) {
  const cwd = activeWorkspace();
  let dimension = item && item.dimension ? item.dimension : item;
  if (purpose === "encryption" || nativeRegistry.providerKey(dimension?.provider) === "minio") {
    throw new Error("MinIO encryption is initialized by Josh Room; Cloudflare is R2-only.");
  }
  const authPurpose = "r2";
  if (!dimension) {
    const catalog = await loadCatalog(cwd, "Loading storage...");
    dimension = dimensionList(catalog).find((candidate) => nativeRegistry.providerKey(candidate.provider) === "r2");
  }
  const targetDimensionId = dimension && (dimension.id || dimension.dimension_id) || "r2";
  if (!targetDimensionId) throw new Error("Choose an R2 Dimension before connecting Cloudflare.");
  selectedDimensionId = targetDimensionId;
  activeAuthAttempt?.cancel();
  const attempt = {
    controller: undefined,
    cancel() {
      this.controller?.cancel();
    },
  };
  activeAuthAttempt = attempt;
  try {
    return await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Connecting Cloudflare...",
        cancellable: true,
      },
      async (progress, parentToken) => {
        const controller = createCancellationController(parentToken);
        attempt.controller = controller;
        const reporter = createVisualReporter("Connecting Cloudflare", operationKind(["auth", "start"]), progress);
        let sessionId;
        try {
          const started = await runJoshRoom(
            ["auth", "start", "--dimension", targetDimensionId, "--purpose", authPurpose], cwd, controller.token, reporter,
          );
          if (controller.token.isCancellationRequested) return "cancelled";
          const authorizationUrl = started.authorization_url || started.authorizationUrl;
          sessionId = started.session_id || started.sessionId;
          if (!authorizationUrl || !sessionId) {
            throw new Error("Cloudflare connection did not return an authorization session.");
          }
          const opened = await vscode.env.openExternal(vscode.Uri.parse(authorizationUrl));
          if (opened === false) {
            throw new Error("Could not open Cloudflare authorization in your local browser.");
          }

          if (controller.token.isCancellationRequested) return "cancelled";
          const result = await runJoshRoom(
            ["auth", "wait", sessionId, "--dimension", targetDimensionId,
              "--purpose", authPurpose,
              "--timeout", String(Math.max(1, Math.ceil(timeoutMs / 1000)))],
            cwd, controller.token, reporter,
          );
          if (controller.token.isCancellationRequested) return "cancelled";
          if (result.status === "authorized") {
            if (roomsProvider) await roomsProvider.setCatalog(await loadCatalog(cwd, "Refreshing Dimensions and Rooms..."));
            await vscode.window.showInformationMessage("Cloudflare connected. Your Rooms are ready.");
            reporter.finish();
            return "connected";
          }
          throw new Error(`Cloudflare authorization ${result.status || "failed"}`);
        } catch (error) {
          const cancelled = controller.token.isCancellationRequested || isCancellationError(error);
          if (cancelled) {
            outputChannel?.info("Cloudflare authorization cancelled; stale attempt invalidated.");
          } else if (sessionId) {
            outputChannel?.warn("Cloudflare authorization failed; invalidating the pending attempt.");
          }
          if (sessionId) {
            await runJoshRoom(["auth", "cancel", sessionId], cwd)
              .catch((cancelError) => outputChannel?.warn(`Unable to invalidate Cloudflare authorization: ${cancelError.message}`));
          }
          if (cancelled) {
            await runJoshRoom(
              ["auth", "status", "--dimension", targetDimensionId], cwd,
            ).catch((statusError) => outputChannel?.warn(`Unable to reconcile cancelled Cloudflare authorization: ${statusError.message}`));
            return "cancelled";
          }
          reporter.fail(error);
          throw error;
        } finally {
          reporter.dispose();
          controller.dispose();
        }
      },
    );
  } finally {
    if (activeAuthAttempt === attempt) activeAuthAttempt = undefined;
  }
}

async function connectEncryption(item, options = {}) {
  assertWorkspaceTrusted("initialize MinIO encryption");
  const cwd = activeWorkspace();
  const dimension = item?.dimension || item;
  const dimensionIdValue = dimension?.id || dimension?.dimension_id || selectedDimensionId;
  if (!dimensionIdValue || nativeRegistry.providerKey(dimension?.provider) !== "minio") {
    throw new Error("Choose a MinIO Dimension before initializing encryption.");
  }
  const handoff = options.recoveryHandoff || (await vscode.window.showOpenDialog({
    title: "Choose recovery identity for MinIO encryption",
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: true,
    canSelectFolders: false,
    canSelectMany: false,
    openLabel: "Use recovery identity",
  }))?.[0]?.fsPath;
  if (!handoff) return "cancelled";
  const result = await runOperation(
    "Initializing MinIO encryption…",
    ["encryption", "initialize", "--dimension", dimensionIdValue, "--recovery-handoff", handoff],
    cwd,
    { cancellable: true },
  );
  const domain = result.encryption_domain_id || dimension.encryption_domain_id;
  const generation = result.key_generation || dimension.key_generation;
  if (!result.encryption_material && domain && generation) {
    const runtimeRoot = process.env.XDG_RUNTIME_DIR || os.tmpdir();
    const candidate = path.join(runtimeRoot, "josh-room", "session", "encryption", domain, `generation-${generation}.identity`);
    if (fs.existsSync(candidate)) result.encryption_material = candidate;
  }
  await cacheEncryptionMaterial(result, { ...dimension, id: dimensionIdValue });
  if (domain && extensionContext?.secrets?.store) {
    const recoveryStat = fs.lstatSync(handoff);
    if (recoveryStat.isFile() && !recoveryStat.isSymbolicLink()
      && (recoveryStat.mode & 0o077) === 0 && recoveryStat.size <= 16 * 1024) {
      await extensionContext.secrets.store(recoverySecretKey(domain), fs.readFileSync(handoff, "utf8"));
    }
  }
  selectedDimensionId = dimensionIdValue;
  roomsProvider && await roomsProvider.refresh();
  await vscode.window.showInformationMessage("MinIO encryption is ready. Cloudflare R2 was not connected.");
  return "initialized";
}

async function runSelectedEncryption(args, cwd, dimension, options = {}) {
  assertWorkspaceTrusted(options.action || "read or change a trusted Room");
  const selected = dimension?.dimension || dimension;
  const provider = nativeRegistry.providerKey(selected?.provider);
  if (provider !== "minio") return runOperation(options.title || "Running Josh Room operation…", args, cwd, options);
  let material = options.encryptionMaterial || await readEncryptionMaterial(selected);
  if (!material && selected?.id) {
    const status = await runJoshRoom(["encryption", "status", "--dimension", selected.id], cwd);
    material = await readEncryptionMaterial({ ...selected, ...status });
  }
  if (!material && !(selected?.encryption_domain_id || selected?.encryptionDomainId)
    && !(selected?.key_generation || selected?.keyGeneration)) {
    return runOperation(options.title || "Running Josh Room operation…", args, cwd, options);
  }
  if (!material) throw new Error("Selected MinIO encryption material is unavailable; initialize or import it first.");
  return runOperation(options.title || "Running Josh Room operation…", args, cwd, {
    ...options,
    dimension: selected,
    encryptionMaterial: material,
  });
}

function selectedDimension(item) {
  return item?.dimension || item;
}

async function migrateEncryption(item) {
  assertWorkspaceTrusted("migrate encryption");
  const cwd = activeWorkspace();
  const dimension = selectedDimension(item);
  const id = dimension?.id || dimension?.dimension_id;
  if (!id || nativeRegistry.providerKey(dimension.provider) !== "minio") {
    throw new Error("Choose a MinIO Dimension before migrating encryption.");
  }
  const plan = await runSelectedEncryption(
    ["encryption", "migrate", "--dimension", id], cwd, dimension,
    { title: "Planning MinIO encryption migration…", action: "migrate encryption" },
  );
  if (plan.status === "committed" || plan.journal_status === "committed") return plan.status || "committed";
  const confirmed = await vscode.window.showWarningMessage(
    `Migrate the legacy encryption domain for ${dimension.display_name || id}? Existing JAT payloads remain unchanged.`,
    { modal: true },
    "Migrate",
  );
  if (confirmed !== "Migrate") return "cancelled";
  const result = await runSelectedEncryption(
    ["encryption", "resume", "--dimension", id], cwd, dimension,
    { title: "Migrating MinIO encryption…", action: "migrate encryption" },
  );
  await roomsProvider?.refresh();
  return result.status || result.journal_status || "migrated";
}

async function resumeEncryption(item) {
  assertWorkspaceTrusted("resume encryption migration");
  const cwd = activeWorkspace();
  const dimension = selectedDimension(item);
  const id = dimension?.id || dimension?.dimension_id;
  if (!id || nativeRegistry.providerKey(dimension.provider) !== "minio") {
    throw new Error("Choose a MinIO Dimension before resuming encryption.");
  }
  const result = await runSelectedEncryption(
    ["encryption", "resume", "--dimension", id], cwd, dimension,
    { title: "Resuming MinIO encryption migration…", action: "resume encryption migration" },
  );
  await roomsProvider?.refresh();
  return result.status || result.journal_status || "resumed";
}

async function exportRecovery(item) {
  assertWorkspaceTrusted("export recovery material");
  const dimension = selectedDimension(item);
  const domain = dimension?.encryption_domain_id || dimension?.encryptionDomainId;
  if (!domain || nativeRegistry.providerKey(dimension.provider) !== "minio") {
    throw new Error("Choose a ready MinIO Dimension before exporting recovery material.");
  }
  const output = await vscode.window.showSaveDialog({
    title: "Export MinIO recovery identity",
    defaultUri: vscode.Uri.file(path.join(activeWorkspace(), "josh-room-recovery.identity")),
    filters: { "Age identity": ["identity", "txt"] },
  });
  if (!output) return "cancelled";
  const recovery = await extensionContext?.secrets?.get?.(recoverySecretKey(domain));
  if (!recovery) throw new Error("Recovery material is unavailable for the selected Dimension.");
  const outputPath = path.resolve(output.fsPath);
  const temporary = path.join(path.dirname(outputPath), `.josh-room-recovery-${process.pid}-${Date.now()}`);
  try {
    fs.writeFileSync(temporary, String(recovery).endsWith("\n") ? String(recovery) : `${recovery}\n`, { mode: 0o600, flag: "wx" });
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, outputPath);
  } finally {
    fs.rmSync(temporary, { force: true });
  }
  await vscode.window.showInformationMessage(`Recovery identity exported to ${output.fsPath}.`);
  return "exported";
}

async function importRecovery(item) {
  assertWorkspaceTrusted("import recovery material");
  const dimension = selectedDimension(item);
  const recovery = (await vscode.window.showOpenDialog({
    title: "Choose MinIO recovery identity",
    defaultUri: vscode.Uri.file(activeWorkspace()),
    canSelectFiles: true,
    canSelectFolders: false,
    canSelectMany: false,
    openLabel: "Use recovery identity",
  }))?.[0]?.fsPath;
  if (!recovery) return "cancelled";
  return connectEncryption(dimension, { recoveryHandoff: recovery });
}

async function configureStorageBucket({ provider, connectionId, dimensionId, connectionMetadata = {}, credentials, cwd }) {
  let bucketResult;
  const commandOptions = credentials === undefined ? {} : { stdin: credentials };
  try {
    bucketResult = await runOperation(
      `Loading ${provider === "r2" ? "Cloudflare R2" : "MinIO"} buckets...`,
      providerTools.bucketCommand("list", { provider, connectionId, dimensionId }),
      cwd,
      commandOptions,
    );
  } catch (error) {
    if (error.result?.error_code === "bucket-list-forbidden") {
      outputChannel?.warn(`${provider} bucket listing unavailable: ${error.message}`);
      bucketResult = { ok: false, ...error.result, forbidden: true };
    } else {
      outputChannel?.error(`${provider} bucket listing unavailable: ${error.message}`);
      throw error;
    }
  }
  const bucketChoice = await vscode.window.showQuickPick(providerTools.bucketChoices(bucketResult || {}), {
    title: `Josh: Choose a ${provider === "r2" ? "Cloudflare R2" : "MinIO"} Bucket`,
    placeHolder: "Create a dedicated Josh Room bucket or choose an existing bucket",
    ignoreFocusOut: true,
  });
  if (!bucketChoice) return "cancelled";
  let bucket = bucketChoice.bucket;
  const target = {
    provider,
    connectionId,
    dimensionId,
  };
  if (bucketChoice.create) {
    bucket = await vscode.window.showInputBox({
      title: `Josh: Create ${provider === "r2" ? "Cloudflare R2" : "MinIO"} Bucket`,
      prompt: "New bucket name (dedicated to Josh Room)",
      value: bucketChoice.bucket || "josh-room",
      ignoreFocusOut: true,
      validateInput: (value) => value.trim() ? undefined : "Enter a bucket.",
    });
    if (!bucket) return "cancelled";
    await runOperation(
      `Creating ${provider === "r2" ? "Cloudflare R2" : "MinIO"} bucket...`,
      providerTools.bucketCommand("create", { ...target, bucket: bucket.trim() }),
      cwd,
      commandOptions,
    );
  } else if (bucketChoice.manual) {
    bucket = await vscode.window.showInputBox({
      title: `Josh: Add ${provider === "r2" ? "Cloudflare R2" : "MinIO"} Storage`,
      prompt: "Bucket name",
      ignoreFocusOut: true,
      validateInput: (value) => value.trim() ? undefined : "Enter a bucket.",
    });
    if (!bucket) return "cancelled";
  }
  bucket = bucket.trim();
  await runOperation(
    `Checking ${provider === "r2" ? "Cloudflare R2" : "MinIO"} bucket access...`,
    providerTools.bucketCommand("check", { ...target, bucket }),
    cwd,
    commandOptions,
  );
  const connectionRef = connectionId ? connectionId : dimensionId;
  const dimensionIdValue = `${provider}-${connectionRef}-${bucket}`
    .toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  const dimensionArgs = [
    "dimensions", "add", dimensionIdValue, "--display-name", bucket,
    "--provider", provider, "--bucket", bucket,
  ];
  if (provider === "minio") dimensionArgs.push("--connection", connectionId);
  else dimensionArgs.push(
    "--endpoint", bucketResult.endpoint || connectionMetadata.endpoint || "",
    "--credential-profile", bucketResult.credential_profile || "oauth-runtime",
    "--region", bucketResult.region || "auto",
  );
  await runOperation(`Adding ${provider === "r2" ? "Cloudflare R2" : "MinIO"} Dimension...`, dimensionArgs, cwd, commandOptions);
  selectedDimensionId = dimensionIdValue;
  if (roomsProvider) await roomsProvider.refresh();
  await vscode.window.showInformationMessage(
    `Connected ${provider === "r2" ? "Cloudflare R2" : "MinIO"} bucket ${bucket}. ${provider === "minio" ? "Credentials were handed to the secure backend." : "Cloudflare authorization remains local to this Room."}`,
  );
  return "added";
}

async function addStorage() {
  assertWorkspaceTrusted("add storage");
  const cwd = activeWorkspace();
  const providerChoice = await vscode.window.showQuickPick([
    { label: "Cloudflare R2", provider: "r2" },
    { label: "MinIO", provider: "minio" },
  ], { title: "Josh: Add Storage", placeHolder: "Choose a storage provider", ignoreFocusOut: true });
  if (!providerChoice) return "cancelled";
  if (providerChoice.provider === "r2") {
    const connected = await connectCloudflare();
    return connected === "connected" ? configureStorageBucket({ provider: "r2", dimensionId: "r2", cwd }) : connected;
  }
  let endpoint;
  let credentials;
  let returnedConnectionId;
  let connectionMetadata;
  let existingConnections = [];
  try {
      existingConnections = providerTools.connectionRecords(
      await runOperation("Loading storage connections...", providerTools.connectionCommand("list"), cwd),
    ).filter((connection) => nativeRegistry.providerKey(connection.provider) === "minio");
  } catch (_error) {
    existingConnections = [];
  }
  const reuseChoice = existingConnections.length > 0
    ? await vscode.window.showQuickPick(existingConnections.map((connection) => ({
      label: connection.display_name || connection.name || connection.id,
      connection,
    })).concat([{ label: "$(add) New MinIO Connection…", create: true }]), {
      title: "Josh: Choose a MinIO Connection", placeHolder: "Reuse a connection or create one", ignoreFocusOut: true,
    })
    : undefined;
  if (reuseChoice && !reuseChoice.create) {
    const connection = reuseChoice.connection || reuseChoice;
    endpoint = connection.endpoint;
    returnedConnectionId = connection.id || connection.connection_id;
    connectionMetadata = connection;
  } else {
    endpoint = await vscode.window.showInputBox({
      title: "Josh: Add MinIO Storage", prompt: "Endpoint URL", placeHolder: "https://...", ignoreFocusOut: true,
      validateInput: (value) => value.trim().startsWith("http://")
        || value.trim().startsWith("https://") ? undefined : "Enter an http(s) endpoint.",
    });
    if (!endpoint) return "cancelled";
    const accessKey = await vscode.window.showInputBox({
      title: "Josh: Add MinIO Storage", prompt: "Access key", password: true, ignoreFocusOut: true,
      validateInput: (value) => value.trim() ? undefined : "Enter an access key.",
    });
    if (!accessKey) return "cancelled";
    const secretKey = await vscode.window.showInputBox({
      title: "Josh: Add MinIO Storage", prompt: "Secret key", password: true, ignoreFocusOut: true,
      validateInput: (value) => value.trim() ? undefined : "Enter a secret key.",
    });
    if (!secretKey) return "cancelled";
    credentials = JSON.stringify({ "access-key-id": accessKey, "secret-access-key": secretKey });
    const created = await runOperation("Connecting MinIO...", providerTools.connectionCommand("create", {
      provider: "minio", endpoint: endpoint.trim(),
    }), cwd, { stdin: credentials });
    const connection = created.connection || created;
    returnedConnectionId = typeof connection === "string" ? connection : connection.id || connection.connection_id;
    connectionMetadata = typeof connection === "object" ? connection : {};
    if (credentials) {
      const parsedCredentials = JSON.parse(credentials);
      const profile = typeof connection === "object" && connection.credential_profile
        ? connection.credential_profile
        : `josh-room-${returnedConnectionId}`;
      await rememberExtensionCredentials(profile, parsedCredentials);
    }
  }
  if (!returnedConnectionId) throw new Error("MinIO connection did not return a connection id.");
  return configureStorageBucket({
    provider: "minio",
    connectionId: returnedConnectionId,
    connectionMetadata: { ...connectionMetadata, endpoint: endpoint.trim(), credential_profile: connectionMetadata.credential_profile || `josh-room-${returnedConnectionId}` },
    credentials,
    cwd,
  });
}

async function connectStorage(item) {
  assertWorkspaceTrusted("connect storage");
  const connection = item?.connection || item;
  if (nativeRegistry.providerKey(item?.provider || connection?.provider) === "r2") {
    return connectCloudflare(item);
  }
  if (!connection?.id && !connection?.connection_id) return addStorage();
  return editConnection(item, { reconnect: true });
}

async function editConnection(item, { reconnect = false } = {}) {
  assertWorkspaceTrusted("edit storage connection");
  const connection = item?.connection || item;
  if (nativeRegistry.providerKey(item?.provider || connection?.provider) === "r2") {
    return connectCloudflare(item);
  }
  const connectionId = connection?.id || connection?.connection_id;
  if (!connectionId) return addStorage();
  const endpoint = await vscode.window.showInputBox({
    title: reconnect ? "Josh: Reconnect MinIO" : "Josh: Edit Connection",
    prompt: "Endpoint URL", value: connection.endpoint || "", ignoreFocusOut: true,
    validateInput: (value) => value.trim().startsWith("http://") || value.trim().startsWith("https://")
      ? undefined : "Enter an http(s) endpoint.",
  });
  if (!endpoint) return "cancelled";
  const accessKey = await vscode.window.showInputBox({ title: "Josh: MinIO Connection", prompt: "Access key", password: true, ignoreFocusOut: true });
  if (!accessKey) return "cancelled";
  const secretKey = await vscode.window.showInputBox({ title: "Josh: MinIO Connection", prompt: "Secret key", password: true, ignoreFocusOut: true });
  if (!secretKey) return "cancelled";
  await runOperation(reconnect ? "Reconnecting MinIO..." : "Updating MinIO connection...", [
    ...providerTools.connectionCommand(reconnect ? "reconnect" : "update", {
      provider: "minio", connectionId, endpoint: endpoint.trim(),
    }),
  ], activeWorkspace(), { stdin: JSON.stringify({ "access-key-id": accessKey, "secret-access-key": secretKey }) });
  await rememberExtensionCredentials(
    connection.credential_profile || `josh-room-${connectionId}`,
    { "access-key-id": accessKey, "secret-access-key": secretKey },
  );
  if (roomsProvider) await roomsProvider.refresh();
  return reconnect ? "connected" : "updated";
}

async function disconnectStorage(item) {
  const connection = item?.connection || item;
  const provider = nativeRegistry.providerKey(item?.provider || connection?.provider);
  const connectionId = connection?.id || connection?.connection_id;
  selectedDimensionId = undefined;
  if (provider === "r2") {
    await runOperation("Disconnecting Cloudflare locally...", ["auth", "logout", "--purpose", "r2"], activeWorkspace());
  } else if (connectionId) {
    await runOperation("Disconnecting locally...", providerTools.connectionCommand("disconnect", { connectionId }), activeWorkspace());
  }
  if (roomsProvider) await roomsProvider.refresh();
  await vscode.window.showInformationMessage("Disconnected locally. Remote buckets and JAT history were not changed.");
  return "disconnected";
}

function selectDimension(item) {
  const dimension = item && item.dimension ? item.dimension : item;
  if (!dimension) return "cancelled";
  selectedDimensionId = dimension.id || dimension.dimension_id;
  return "selected";
}

async function loadNativeCatalog(cwd, title) {
  try {
    const catalog = await runOperation(title || "Loading your Rooms...", ["dimensions", "list"], cwd);
    const dimensions = dimensionList(catalog);
    for (const dimension of dimensions) {
      if (Array.isArray(dimension.projects) || Array.isArray(dimension.rooms)) continue;
      try {
        const projects = await runOperation(title || "Loading your Rooms...",
          nativeRegistry.dimensionArgs(["projects", "list", "--backend", dimension.provider || "r2"],
            dimension.id || dimension.dimension_id), cwd);
        dimension.projects = projects.projects || [];
      } catch (error) {
        outputChannel && outputChannel.warn("Unable to load Rooms for Dimension " + dimension.id + ": " + error.message);
        dimension.projects = [];
      }
    }
    return Object.assign({}, catalog, { dimensions });
  } catch (_error) {
    return legacyLoadCatalog(cwd, title);
  }
}

class HierarchyRoomsProvider {
  constructor() {
    this.roots = undefined;
    this.state = "initial";
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  getTreeItem(item) {
    if (item.kind === "runtime") {
      const treeItem = new vscode.TreeItem(item.label, vscode.TreeItemCollapsibleState.None);
      treeItem.description = item.description || "";
      treeItem.iconPath = new vscode.ThemeIcon(item.failed ? "error" : "sync~spin");
      treeItem.command = { command: "joshRoom.refresh", title: item.failed ? "Retry runtime preparation" : "Show runtime progress" };
      return treeItem;
    }
    const emptyKind = ["load", "loading", "empty", "error"].includes(item.kind);
    if (item.kind === "dimension-error") {
      const treeItem = new vscode.TreeItem(
        item.label || "Could not load bucket — Retry",
        vscode.TreeItemCollapsibleState.None,
      );
      treeItem.description = item.description || "";
      treeItem.contextValue = "dimension-error";
      treeItem.iconPath = new vscode.ThemeIcon("error");
      const action = item.error?.action;
      const command = action === "edit"
        ? "joshRoom.editConnection"
        : action === "reconnect" ? "joshRoom.reconnectStorage"
          : action === "authorize" ? "joshRoom.connectEncryption" : "joshRoom.refresh";
      treeItem.command = {
        command,
        title: item.label || "Retry",
        ...(command === "joshRoom.refresh" ? {} : { arguments: [item.connection || item.dimension] }),
      };
      return treeItem;
    }
    if (emptyKind) {
      const labels = {
        load: "Load Storage",
        loading: "Loading Storage...",
        empty: "No storage connected",
        error: "Could not load storage - click to retry",
      };
      const treeItem = new vscode.TreeItem(labels[item.kind], vscode.TreeItemCollapsibleState.None);
      treeItem.iconPath = new vscode.ThemeIcon(item.kind === "loading" ? "sync~spin" : item.kind === "error" ? "error" : "cloud-download");
      treeItem.command = { command: item.kind === "empty" ? "joshRoom.addStorage" : "joshRoom.refresh", title: labels[item.kind] };
      return treeItem;
    }
    const hasChildren = Array.isArray(item.children) && item.children.length > 0;
    const state = hasChildren
      ? ["provider", "dimension", "connection"].includes(item.kind)
        ? vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.Collapsed
      : vscode.TreeItemCollapsibleState.None;
    const treeItem = new vscode.TreeItem(item.label || item.id, state);
    treeItem.id = [item.kind, dimensionId(item), item.id].filter(Boolean).join(":");
    const syntheticDimension = item.kind === "dimension" && item.dimension?.synthetic;
    treeItem.contextValue = item.kind === "connection"
      ? item.state === "expired" ? "provider-connection-expired"
        : item.state === "connected" ? "provider-connection-connected" : "provider-connection"
      : syntheticDimension ? "dimension-synthetic"
        : item.kind === "dimension" && item.encryption_state ? `dimension-${item.encryption_state}` : item.kind;
    treeItem.description = item.description || "";
    treeItem.iconPath = new vscode.ThemeIcon(
      item.kind === "provider" ? "cloud"
        : item.kind === "dimension" ? "database"
          : item.kind === "connection" ? item.state === "connected" ? "pass-filled" : item.state === "expired" || item.state === "disconnected" ? "warning" : "plug"
            : item.kind === "room" ? "archive" : "history",
    );
    if (item.kind === "dimension") {
      treeItem.command = item.action
        ? { ...item.action, arguments: [item] }
        : { command: "joshRoom.selectDimension", title: "Select Storage", arguments: [item] };
    }
    if (item.kind === "connection") {
      const command = item.state === "expired" || item.state === "disconnected"
        ? "joshRoom.reconnectStorage"
        : "joshRoom.connectStorage";
      const title = item.state === "expired" || item.state === "disconnected" ? "Reconnect" : "Connect";
      treeItem.command = { command, title, arguments: [item] };
    }
    if (item.kind === "room") {
      let current;
      try { current = currentRoom(activeWorkspace()); } catch (_error) {}
      if (!sameRoomBinding(current, item)) {
        treeItem.command = { command: "joshRoom.enter", title: "Enter Room", arguments: [item] };
      }
    }
    if (item.kind === "jat") {
      treeItem.command = { command: "joshRoom.enter", title: "Enter JAT", arguments: [item] };
    }
    return treeItem;
  }

  getChildren(item) {
    if (item) return item.children || [];
    if (runtimeLifecycle.state !== "RUNTIME_READY") {
      return [{
        kind: "runtime",
        label: runtimeLifecycle.state === "FAILED" ? "Josh Room runtime unavailable — Retry" : "Preparing Josh Room runtime…",
        description: runtimeLifecycle.message,
        failed: runtimeLifecycle.state === "FAILED",
      }];
    }
    if (this.state === "loading") return [{ kind: "loading" }];
    if (this.state === "error") return [{ kind: "error" }];
    if (this.state === "initial") return [{ kind: "load" }];
    return this.roots && this.roots.length ? this.roots : [{ kind: "empty" }];
  }

  async setCatalog(catalog) {
    lastKnownCatalog = catalog;
    this.roots = nativeRegistry.buildProviderTree(catalog || {});
    this.state = "ready";
    await vscode.commands.executeCommand("setContext", "joshRoom.roomsEmpty", this.roots.length === 0);
    this.emitter.fire(undefined);
  }

  async refresh() {
    if (lastKnownCatalog) {
      await this.setCatalog(lastKnownCatalog);
    } else {
      this.state = "initial";
    }
    this.emitter.fire(undefined);
    return lastKnownCatalog;
  }
}

const legacyLoadCatalog = loadCatalog;
loadCatalog = loadNativeCatalog;
const legacyCurrentRoom = currentRoom;
currentRoom = function nativeCurrentRoom(cwd) {
  const marker = legacyCurrentRoom(cwd);
  if (marker) return marker;
  try {
    const value = JSON.parse(fs.readFileSync(path.join(cwd, ".josh-room.json"), "utf8"));
    return isRoomMarker(value) ? value : undefined;
  } catch (_error) {
    return undefined;
  }
};

let baselineLoading = false;
let pendingWorkspaceEvents = [];
let workspaceBindingTrusted = false;
const legacyStartDirtyTracking = startDirtyTracking;
startDirtyTracking = async function nativeStartDirtyTracking(context) {
  const root = activeWorkspace();
  const marker = currentRoom(root);
  selectedDimensionId = marker && marker.dimension_id;
  if (!marker) return legacyStartDirtyTracking(context);
  const generation = ++dirtyTrackingGeneration;
  workspaceWatcher && workspaceWatcher.dispose();
  pendingWorkspaceEvents = [];
  baselineLoading = true;
  const savedFingerprint = marker.workspace_fingerprint;
  const fingerprintProvider = async () => {
    const live = await runJoshRoom(["status"], root);
    return live.current_workspace_fingerprint || live.current_fingerprint || live.workspace_fingerprint;
  };
  workspaceBindingTrusted = false;
  workspaceBaseline = new WorkspaceBaseline(root, {
    savedFingerprint,
    fingerprintProvider,
  });
  for (const document of vscode.workspace.textDocuments || []) {
    const relative = relativeWorkspacePath(document.uri);
    if (relative && document.isDirty) dirtyBuffers.add(relative);
  }
  workspaceWatcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(root, "**/*"));
  const compare = (uri) => baselineLoading ? pendingWorkspaceEvents.push(uri) : markWorkspaceChange(uri);
  workspaceWatcher.onDidChange(compare);
  workspaceWatcher.onDidCreate(compare);
  workspaceWatcher.onDidDelete(compare);
  context.subscriptions.push(workspaceWatcher);
  context.subscriptions.push(vscode.workspace.onDidRenameFiles((event) => {
    for (const file of event.files || []) {
      compare(file.oldUri);
      compare(file.newUri);
    }
  }));
  const statusPromise = runJoshRoom(["status"], root)
    .then((result) => result)
    .catch((error) => {
      outputChannel && outputChannel.warn("Auth-free Room status unavailable: " + error.message);
      return undefined;
    });
  await workspaceBaseline.capture();
  if (generation !== dirtyTrackingGeneration) return;
  const initialStatus = await statusPromise;
  if (generation !== dirtyTrackingGeneration) return;
  const status = await runJoshRoom(["status"], root).catch((error) => {
    outputChannel && outputChannel.warn("Auth-free Room status unavailable: " + error.message);
    return undefined;
  });
  if (generation !== dirtyTrackingGeneration) return;
  const authoritativeStatus = status || initialStatus;
  const currentFingerprint = status && (
    status.current_workspace_fingerprint || status.current_fingerprint || status.workspace_fingerprint
  );
  workspaceBindingTrusted = Boolean(authoritativeStatus
    && authoritativeStatus.ok
    && authoritativeStatus.path_matches
    && authoritativeStatus.state === "clean"
    && authoritativeStatus.fingerprint_matches !== false);
  await workspaceBaseline.capture({
    savedFingerprint,
    currentFingerprint: currentFingerprint || savedFingerprint,
    fingerprintProvider,
  });
  if (generation !== dirtyTrackingGeneration) return;
  baselineLoading = false;
  for (const uri of pendingWorkspaceEvents.splice(0)) await markWorkspaceChange(uri);
  setRoomDirty(workspaceBaseline.dirty.size > 0 || dirtyBuffers.size > 0);
  refreshRoomStatus();
};

module.exports.__test__.startDirtyTracking = startDirtyTracking;

async function resetNativeBaseline(result) {
  if (!workspaceBaseline) return;
  let fingerprint = result && (result.workspace_fingerprint || result.current_workspace_fingerprint);
  if (!fingerprint && workspaceBaseline.fingerprintProvider) {
    try {
      fingerprint = await workspaceBaseline.fingerprintProvider();
    } catch (error) {
      outputChannel && outputChannel.warn("Unable to refresh authoritative Room status: " + error.message);
    }
  }
  const marker = currentRoom(activeWorkspace());
  const savedFingerprint = result && result.saved_workspace_fingerprint
    || marker && marker.workspace_fingerprint
    || fingerprint;
  workspaceBindingTrusted = true;
  await workspaceBaseline.capture({ savedFingerprint, currentFingerprint: fingerprint || savedFingerprint });
  dirtyBuffers.clear();
  setRoomDirty(false);
  refreshRoomStatus();
}

function routeItemArgs(args, item) {
  return nativeRegistry.dimensionArgs(args, dimensionId(item));
}

async function explicitRoomContext(preferredProject, title) {
  const catalog = await loadCatalog(activeWorkspace(), title);
  const projects = nativeRegistry.flattenDimensionRooms(catalog);
  const preferred = roomProject(preferredProject);
  const preferredDimensionId = explicitDimensionId(preferredProject);
  const preferredSnapshotId = snapshotIdentity(preferredProject);
  if (preferred && preferredDimensionId) {
    const catalogProject = projects.find((project) => project.id === preferred.id
      && explicitDimensionId(project) === preferredDimensionId);
    const catalogSnapshotIds = catalogProject && (catalogProject.snapshots || catalogProject.jats || [])
      .map((snapshot) => snapshot.snapshot_id || snapshot.id);
    const snapshotId = preferredSnapshotId || latestSnapshotId(catalogProject);
    if (catalogProject && snapshotId && (!preferredSnapshotId || catalogSnapshotIds.includes(snapshotId))) {
      return { ...catalogProject, project_id: catalogProject.id, snapshot_id: snapshotId };
    }
  }
  const choices = projects.flatMap((project) => {
    const snapshots = Array.isArray(project.snapshots) ? project.snapshots
      : Array.isArray(project.jats) ? project.jats : [];
    return snapshots.map((snapshot) => ({
      label: `${roomLabel(project)} · ${snapshot.display_name || snapshot.name || snapshot.snapshot_id}`,
      description: snapshot.created_at || snapshot.snapshot_id,
      project,
      snapshot,
      snapshotId: snapshot.snapshot_id || snapshot.id,
    }));
  });
  const choice = await vscode.window.showQuickPick(
    choices,
    { title, placeHolder: "Choose a trusted Room recovery point", ignoreFocusOut: true },
  );
  if (!choice) return undefined;
  const project = roomProject(choice.project || choice);
  const dimension = explicitDimensionId(choice) || explicitDimensionId(project);
  const snapshotId = snapshotIdentity(choice) || latestSnapshotId(project);
  if (!project?.id || !dimension || !snapshotId) {
    throw new Error("Choose a trusted Dimension, Room, and JAT recovery point.");
  }
  return { ...project, project_id: project.id, dimension_id: dimension, snapshot_id: snapshotId };
}

function latestSnapshotId(project) {
  if (project.latest) return project.latest;
  const snapshots = Array.isArray(project.snapshots) ? project.snapshots
    : Array.isArray(project.jats) ? project.jats : [];
  return snapshots[0] && (snapshots[0].snapshot_id || snapshots[0].id);
}

function contextSnapshot(context) {
  const snapshots = Array.isArray(context?.snapshots) ? context.snapshots
    : Array.isArray(context?.jats) ? context.jats : [];
  return snapshots.find((snapshot) => (snapshot.snapshot_id || snapshot.id) === context.snapshot_id);
}

function requiresLegacyLinkVerification(context, marker) {
  const snapshot = contextSnapshot(context);
  const fingerprint = snapshot && snapshot.workspace_fingerprint;
  return marker?.format_version === 1
    || !fingerprint
    || !/^[0-9a-f]{64}$/.test(fingerprint)
    || fingerprint === "0".repeat(64);
}

async function verifyLegacyLink(cwd, context) {
  const verificationRoot = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-link-verify-"));
  try {
    await runOperation("Verifying saved JAT...", nativeRegistry.dimensionArgs([
      "hydrate", context.project_id, "--snapshot", context.snapshot_id,
      "--destination", verificationRoot, "--ide", "terminal",
    ], context.dimension_id), cwd);
    const [trusted, current] = await Promise.all([
      runJoshRoom(["status", "--workspace", verificationRoot], cwd),
      runJoshRoom(["status", "--workspace", cwd], cwd),
    ]);
    const trustedFingerprint = trusted.current_workspace_fingerprint
      || trusted.current_fingerprint || trusted.workspace_fingerprint;
    const currentFingerprint = current.current_workspace_fingerprint
      || current.current_fingerprint || current.workspace_fingerprint;
    if (!trustedFingerprint || !currentFingerprint || trustedFingerprint !== currentFingerprint) {
      await vscode.window.showWarningMessage(
        "This folder does not match that saved Room. Enter the Room instead, or save this folder as a new Room.",
        "Enter", "Save as New",
      );
      return { ok: false, mismatch: true };
    }
    return { ok: true, fingerprint: trustedFingerprint };
  } catch (error) {
    outputChannel?.warn("Legacy Link verification failed: " + error.message);
    await vscode.window.showErrorMessage(
      "Josh Room could not verify that saved JAT. Enter the Room instead, or save this folder as a new Room.",
    );
    return { ok: false, failed: true };
  } finally {
    fs.rmSync(verificationRoot, { recursive: true, force: true });
  }
}

async function linkRoom(preferredProject) {
  assertWorkspaceTrusted("link a Room");
  const cwd = activeWorkspace();
  const context = await explicitRoomContext(preferredProject, "Josh: Link Existing Folder");
  if (!context) return "cancelled";
  const marker = currentRoom(cwd);
  let verifiedFingerprint;
  if (requiresLegacyLinkVerification(context, marker)) {
    const verification = await verifyLegacyLink(cwd, context);
    if (!verification.ok) return verification.mismatch ? "mismatch" : "verification-failed";
    verifiedFingerprint = verification.fingerprint;
  }
  const args = ["link", "--project", context.project_id, "--snapshot", context.snapshot_id];
  if (verifiedFingerprint) args.push("--workspace-fingerprint", verifiedFingerprint);
  const routed = routeItemArgs(args, context);
  const result = nativeRegistry.providerKey(context.provider || context.dimension?.provider) === "minio"
    ? await runSelectedEncryption(routed, cwd, context.dimension || context, { title: "Linking saved Room…", action: "link a Room" })
    : await runOperation("Linking saved Room...", routed, cwd);
  await resetNativeBaseline(result);
  if (roomsProvider) await roomsProvider.refresh();
  return "linked";
}

async function repairRoom(preferredProject) {
  assertWorkspaceTrusted("repair a Room");
  const cwd = activeWorkspace();
  const context = await explicitRoomContext(preferredProject, "Josh: Repair Room Ledger");
  if (!context) return "cancelled";
  const args = ["repair", "--project", context.project_id, "--snapshot", context.snapshot_id];
  const routed = routeItemArgs(args, context);
  const result = nativeRegistry.providerKey(context.provider || context.dimension?.provider) === "minio"
    ? await runSelectedEncryption(routed, cwd, context.dimension || context, { title: "Repairing Room ledger…", action: "repair a Room" })
    : await runOperation("Repairing Room ledger...", routed, cwd);
  await resetNativeBaseline(result);
  if (roomsProvider) await roomsProvider.refresh();
  return "repaired";
}

function snapshotCopyArgs(source, target) {
  const sourceDimension = dimensionId(source);
  const targetDimension = dimensionId(target) || sourceDimension;
  const args = ["snapshot", "copy"];
  if (source && source.kind === "jat") {
    args.push(source.project && source.project.id);
    args.push(source.id || source.snapshot && source.snapshot.snapshot_id);
  } else {
    args.push("--source", source && source.path);
  }
  if (sourceDimension) args.push("--dimension", sourceDimension);
  if (targetDimension) args.push("--destination-dimension", targetDimension);
  if (target && target.kind === "room") args.push("--destination-project", target.id);
  return args;
}

class RoomDragAndDropController {
  constructor() {
    this.dragMimeTypes = ["application/vnd.code.tree.joshRoom"];
    this.dropMimeTypes = ["application/vnd.code.tree.joshRoom", "text/uri-list"];
  }

  handleDrag(source, dataTransfer) {
    dataTransfer.set("application/vnd.code.tree.joshRoom", new vscode.DataTransferItem(JSON.stringify(source)));
  }

  async handleDrop(target, dataTransfer) {
    let source;
    const tree = dataTransfer.get("application/vnd.code.tree.joshRoom");
    if (tree) {
      source = JSON.parse(await tree.asString());
      if (Array.isArray(source)) source = source[0];
    } else {
      const uri = dataTransfer.get("text/uri-list");
      if (!uri) return;
      const first = (await uri.asString()).split(/\r?\n/).find((line) => line.trim() && !line.trim().startsWith("#"));
      if (!first) return;
      const value = first.trim();
      let sourcePath;
      try {
        sourcePath = decodeURIComponent(new URL(value).pathname);
      } catch (_error) {
        sourcePath = decodeURIComponent(value.replace(/^file:\/\//, ""));
      }
      source = { kind: "folder", path: sourcePath };
    }
    let destination = target || await chooseDimension(
      await loadCatalog(activeWorkspace()), "Copy JAT to Dimension",
    );
    if (!destination) return "cancelled";
    if (!target || target.kind === "dimension") {
      const roomName = await vscode.window.showInputBox({
        title: "Copy JAT to Room",
        prompt: "Destination Room id or name",
        placeHolder: "room-name",
        validateInput: (value) => value.trim() ? undefined : "Enter a Room id or name.",
      });
      if (!roomName) return "cancelled";
      destination = Object.assign({}, destination, { destination_room: roomName.trim() });
    }
    const result = await runOperation("Copying saved JAT...", snapshotCopyArgs(source, destination), activeWorkspace());
    if (roomsProvider) await roomsProvider.refresh();
    await vscode.window.showInformationMessage("Copied " + (source.label || source.path) + " as a new JAT.");
    return result;
  }
}

module.exports.snapshotCopyArgs = snapshotCopyArgs;
snapshotCopyArgs = nativeRegistry.snapshotCopyArgs;

function activateNative(context) {
  extensionContext = context;
  outputChannel = vscode.window.createOutputChannel("Josh Room", { log: true });
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusItem.command = "workbench.view.extension.josh-room";
  setStatus("$(archive) Josh Room");
  statusItem.show();
  roomsProvider = new HierarchyRoomsProvider();
  const roomsView = vscode.window.createTreeView("joshRoom.rooms", {
    treeDataProvider: roomsProvider,
    dragAndDropController: new RoomDragAndDropController(),
  });
  const jatToolsView = vscode.window.createTreeView("joshRoom.jatTools", {
    treeDataProvider: new JatToolsProvider(),
  });
  context.subscriptions.push(outputChannel, statusItem, roomsView, jatToolsView);
  if (context.secrets?.onDidChange) {
    context.subscriptions.push(context.secrets.onDidChange((event) => {
      if (!event?.key || event.key.startsWith(ENCRYPTION_SECRET_PREFIX) || event.key.startsWith(RECOVERY_SECRET_PREFIX)) {
        roomsProvider.roots = undefined;
        roomsProvider.emitter.fire(undefined);
        refreshRoomStatus();
      }
    }));
  }
  context.subscriptions.push(vscode.workspace.onDidChangeTextDocument((event) => {
    const relative = relativeWorkspacePath(event.document.uri);
    if (!relative) return;
    if (event.document.isDirty) dirtyBuffers.add(relative);
    else dirtyBuffers.delete(relative);
    setRoomDirty(!workspaceBaseline || workspaceBaseline.dirty.size > 0 || dirtyBuffers.size > 0);
  }));
  context.subscriptions.push(vscode.workspace.onDidSaveTextDocument((document) => {
    const relative = relativeWorkspacePath(document.uri);
    if (!relative) return;
    dirtyBuffers.delete(relative);
    if (workspaceBaseline) markWorkspaceChange(document.uri);
  }));
  context.subscriptions.push(vscode.workspace.onDidCloseTextDocument((document) => {
    const relative = relativeWorkspacePath(document.uri);
    if (!relative) return;
    dirtyBuffers.delete(relative);
    if (workspaceBaseline) markWorkspaceChange(document.uri);
  }));
  register(context, "joshRoom.save", saveRoom);
  register(context, "joshRoom.new", () => saveRoom({ forceCreate: true }));
  register(context, "joshRoom.addStorage", addStorage);
  register(context, "joshRoom.connectCloudflare", connectCloudflare);
  register(context, "joshRoom.reconnectCloudflare", connectCloudflare);
  register(context, "joshRoom.connectEncryption", connectEncryption);
  register(context, "joshRoom.initializeEncryption", connectEncryption);
  register(context, "joshRoom.migrateEncryption", migrateEncryption);
  register(context, "joshRoom.resumeEncryption", resumeEncryption);
  register(context, "joshRoom.exportRecovery", exportRecovery);
  register(context, "joshRoom.importRecovery", importRecovery);
  register(context, "joshRoom.connectStorage", connectStorage);
  register(context, "joshRoom.reconnectStorage", (item) => connectStorage(item));
  register(context, "joshRoom.editConnection", editConnection);
  register(context, "joshRoom.disconnectStorage", disconnectStorage);
  register(context, "joshRoom.selectDimension", selectDimension);
  register(context, "joshRoom.editStorageSettings", editStorageSettings);
  register(context, "joshRoom.enter", enterRoom);
  register(context, "joshRoom.link", linkRoom);
  register(context, "joshRoom.repair", repairRoom);
  register(context, "joshRoom.remove", removeRoom);
  register(context, "joshRoom.serve", serveRoom);
  register(context, "joshRoom.refresh", () => roomsProvider.refresh());
  register(context, "joshRoom.jatBuild", jatBuild);
  register(context, "joshRoom.jatInspect", jatInspect);
  register(context, "joshRoom.jatExtract", jatExtract);
  register(context, "joshRoom.jatRestore", jatRestore);
  register(context, "joshRoom.jatServe", jatServe);
  register(context, "joshRoom.jatExport", jatExport);
  register(context, "joshRoom.jatCopy", jatCopy);
  // Activation only registers native UI. Runtime, storage, auth, and key
  // reads begin after an explicit command or tree load action.
}

module.exports.activate = activateNative;

function dimensionLoadFailure(error) {
  const result = error && error.result || {};
  const diagnostic = result.error || error?.message || "unknown storage error";
  const code = result.error_code || "dimension-load-failed";
  if (/encryption-authorization-required/i.test(code + " " + diagnostic)) {
    return {
      code,
      label: "Encryption authorization required — Authorize",
      description: "Authorize Josh Room encryption before reading this Room catalog.",
      action: "authorize",
    };
  }
  if (/bucket|accessdenied|forbidden/i.test(code + " " + diagnostic)) {
    return {
      code,
      label: "Bucket access denied — Edit connection",
      description: "Edit the provider connection or choose another bucket.",
      action: "edit",
    };
  }
  if (/auth|credential|unauthoriz|expired/i.test(code + " " + diagnostic)) {
    return {
      code,
      label: "Authentication failed — Reconnect",
      description: "Reconnect this provider connection to load the bucket.",
      action: "reconnect",
    };
  }
  if (/catalog|decrypt|age|fingerprint/i.test(code + " " + diagnostic)) {
    return {
      code,
      label: "Catalog unavailable — Details",
      description: "The bucket is reachable, but its Room catalog could not be verified.",
      action: "details",
    };
  }
  if (/network|unavailable|timeout|endpoint|connection|refused/i.test(code + " " + diagnostic)) {
    return {
      code,
      label: "Server unavailable — Retry",
      description: "The provider endpoint could not be reached.",
      action: "retry",
    };
  }
  return {
    code,
    label: "Could not load bucket — Retry",
    description: "Refresh this storage or edit its provider connection.",
    action: "retry",
  };
}

async function loadDimensionHierarchy(cwd, dimension, title) {
  const id = dimension.id || dimension.dimension_id;
  const listed = await runOperation(
    title || "Loading your Rooms...",
    nativeRegistry.dimensionArgs(["dimensions", "list", "--with-hierarchy"], id),
    cwd,
  );
  const records = dimensionList(listed);
  const hierarchy = records.find((candidate) => (candidate.id || candidate.dimension_id) === id) || records[0];
  if (!hierarchy || (!Object.prototype.hasOwnProperty.call(hierarchy, "rooms")
    && !Object.prototype.hasOwnProperty.call(hierarchy, "projects"))) {
    throw new Error("complete Dimension hierarchy was not returned");
  }
  return Object.assign({}, dimension, hierarchy, { catalog_complete: true });
}

async function loadNativeCatalogWithSnapshots(cwd, title) {
  const catalog = await runOperation(title || "Loading your Rooms...", ["dimensions", "list"], cwd);
  const dimensions = dimensionList(catalog);
  let connections = catalog.connections;
  let connectionLookupFailed = false;
  if (!connections && (!dimensions.length || dimensions.some((dimension) => dimension.connection_id || dimension.connectionId))) {
    try {
      const listedConnections = await runOperation(title || "Loading your Rooms...", providerTools.connectionCommand("list"), cwd);
      connections = listedConnections.connections || connections;
    } catch (error) {
      connectionLookupFailed = true;
      outputChannel && outputChannel.warn("Unable to load provider connections: " + error.message);
    }
  }
  const providerConnections = providerTools.connectionRecords({ connections });
  const r2Dimension = dimensions.find((candidate) => nativeRegistry.providerKey(candidate.provider) === "r2");
  const legacyCatalog = !dimensions.length && (catalog.dimension_id || Array.isArray(catalog.projects));
  const offerSyntheticR2 = !dimensions.length && !legacyCatalog && !connectionLookupFailed && !providerConnections.length;
  const authDimensionId = r2Dimension && (r2Dimension.id || r2Dimension.dimension_id)
    || (offerSyntheticR2 ? "r2" : undefined);
  const authState = authDimensionId ? await runOperation(
    title || "Loading your Rooms...",
    ["auth", "status", "--dimension", authDimensionId],
    cwd,
    { cancellable: false },
  ).catch((error) => {
    outputChannel && outputChannel.warn("Cloudflare connection state unavailable: " + error.message);
    return { state: "missing", encryption_state: "missing", r2_state: "missing" };
  }) : { state: "missing", encryption_state: "missing", r2_state: "missing" };
  const r2State = authState.r2_state || authState.state || "missing";
  const encryptionState = authState.encryption_state || authState.state || "missing";
  const hasR2 = dimensions.some((dimension) => nativeRegistry.providerKey(dimension.provider) === "r2");
  if (!hasR2 && offerSyntheticR2) {
    dimensions.push({
      id: "r2",
      display_name: "Default",
      provider: "r2",
      synthetic: true,
      connection_state: r2State,
    });
  }
  const connectionStates = new Map(providerConnections.map((connection) => [
    connection.id || connection.connection_id,
    connection.auth_state === "disconnected" || connection.connection_state === "disconnected"
      ? "disconnected"
      : connection.auth_state === "expired" || connection.connection_state === "expired"
        ? "expired"
        : undefined,
  ]));
  const loaded = [];
  for (const original of dimensions) {
    const id = original.id || original.dimension_id;
    if (!id) {
      outputChannel && outputChannel.warn("Skipping a Dimension without dimension_id.");
      continue;
    }
    const dimension = Object.assign({}, original);
    const provider = nativeRegistry.providerKey(dimension.provider);
    const connectionState = connectionStates.get(dimension.connection_id || dimension.connectionId);
    if (connectionState) dimension.connection_state = connectionState;
    if (dimension.connection_state && dimension.connection_state !== "connected") {
      loaded.push(dimension);
      continue;
    }
    if (provider === "r2") {
      dimension.connection_state = r2State || dimension.connection_state || "connected";
      if (dimension.connection_state !== "connected") {
        loaded.push(dimension);
        continue;
      }
    }
    if (Object.prototype.hasOwnProperty.call(dimension, "rooms")
      || Object.prototype.hasOwnProperty.call(dimension, "projects")) {
      loaded.push(dimension);
      continue;
    }
    try {
      loaded.push(await loadDimensionHierarchy(cwd, dimension, title));
    } catch (error) {
      const failure = dimensionLoadFailure(error);
      outputChannel && outputChannel.warn("Unable to load Dimension " + id + ": " + (error.result?.error || error.message));
      loaded.push(Object.assign(dimension, { state: "error", load_error: failure }));
    }
  }
  const loadedCatalog = Object.assign({}, catalog, {
    dimensions: loaded,
    ...(connections ? { connections } : {}),
    offer_synthetic_r2: offerSyntheticR2,
    auth_state: r2State,
    encryption_state: encryptionState,
    projects: nativeRegistry.flattenDimensionRooms({ ...catalog, dimensions: loaded }),
  });
  lastKnownCatalog = loadedCatalog;
  return loadedCatalog;
}

loadNativeCatalog = loadNativeCatalogWithSnapshots;
loadCatalog = loadNativeCatalogWithSnapshots;

async function editStorageSettings(item) {
  assertWorkspaceTrusted("edit storage settings");
  const selected = item && item.dimension ? item.dimension : item;
  const catalog = selected ? undefined : await loadCatalog(activeWorkspace(), "Loading storage...");
  const dimension = selected || await chooseDimension(catalog, "Edit Settings");
  if (!dimension) return "cancelled";
  if (dimension.synthetic && nativeRegistry.providerKey(dimension.provider) === "r2") {
    await vscode.window.showInformationMessage(
      "Default Cloudflare storage is managed by OAuth. Use Connect Cloudflare instead.",
    );
    return "cancelled";
  }
  let connection = item?.connection || dimension.connection;
  if (!connection && dimension.connection_id) {
    const resolvedCatalog = catalog || await loadCatalog(activeWorkspace(), "Loading storage...");
    connection = providerTools.dimensionConnection(dimension, providerTools.connectionRecords(resolvedCatalog));
  }
  return editConnection({ connection: connection || dimension, provider: (connection || dimension).provider });
}

Object.assign(module.exports.__test__, {
  JAT_SERVE_MODES,
  JatToolsProvider,
  waitForServeEndpoint,
  jatBuild,
  jatInspect,
  jatExtract,
  jatServe,
  jatExport,
  jatCopy,
  chooseHaul,
  inventoryQuickPickItems,
  isHaulerChunkSize,
  isSafeCopyTarget,
  resultPayloads,
  chooseServeProject,
  addStorage,
  connectCloudflare,
  connectEncryption,
  runSelectedEncryption,
  migrateEncryption,
  resumeEncryption,
  exportRecovery,
  importRecovery,
  encryptionSecretKey,
  recoverySecretKey,
  connectStorage,
  disconnectStorage,
  editStorageSettings,
  editConnection,
  HierarchyRoomsProvider,
  linkRoom,
  loadCatalog,
  repairRoom,
  removeSnapshot,
  RoomDragAndDropController,
  roomLabel,
  runJoshRoom,
  runOperation,
  chooseLocalFallback,
  localRuntimeState,
  initializeManagedRuntime,
  operationNeedsJat,
  clearLocalFallback,
  buildTerminalLaunch,
  isSafeRestoreName,
  terminateChild,
  serveRoom,
  selectDimension,
  setRuntimeForTests(value) {
    testRuntime = value;
  },
  setExtensionContextForTests(value) {
    extensionContext = value;
    managedRuntimePromise = undefined;
    managedJatPromise = undefined;
    runtimeLifecycle = { state: "UNINITIALIZED", message: "Preparing Josh Room runtime…" };
  },
  setRuntimeReadinessForTests(value) {
    runtimeReadinessOverride = value;
    managedRuntimePromise = undefined;
    managedJatPromise = undefined;
    runtimeLifecycle = { state: "UNINITIALIZED", message: "Preparing Josh Room runtime…" };
  },
  setRoomsProvider(value) {
    roomsProvider = value;
  },
});
