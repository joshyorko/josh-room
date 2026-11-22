const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");
const { WorkspaceBaseline, isRoomMarker, loadCapturePolicy, shouldMarkDirty } = require("./dirty");
const managedRuntime = require("./runtime");
const {
  createProgressTracker,
  followProgressFile,
  formatProgressDisplay,
  operationKind,
} = require("./progress");
const { followLogFile, stageForLog, waitForServeStartup, probeRegistry, readServeJson } = require("./registry");
const providerTools = require("./provider");

let outputChannel;
let roomsProvider;
let statusItem;
let activeOperationId = 0;
let extensionContext;
let roomStatus = Object.freeze({ kind: "unknown" });
let workspaceBaseline;
let workspaceWatcher;
let capturePolicyWatcher;
let workspaceCapturePolicy;
let dirtyTrackingGeneration = 0;
let workspaceEventGeneration = 0;
let cleanEventGeneration = -1;
let trustedSaveReceipt;
const dirtyBuffers = new Set();
let activeAuthAttempt;
let managedRuntimePromise;
let runtimePreparation;
let managedJatPromise;
let jatPreparation;
let runtimeReadinessOverride;
let runtimeLifecycle = { state: "UNINITIALIZED", message: "Select to prepare the runtime and load storage" };
let testRuntime;
const CREDENTIALS_SECRET = "josh-room.credentials.v1";
const ENCRYPTION_SECRET_PREFIX = "josh-room.encryption.v1:";
const RECOVERY_SECRET_PREFIX = "josh-room.recovery.v1:";
const INSTALLATION_RECOVERY_SECRET = "josh-room.recovery.v1";

function usesNativeEncryption(provider) {
  return ["minio", "s3"].includes(nativeRegistry.providerKey(provider));
}

function nativeEncryptionLabel(provider) {
  return nativeRegistry.providerKey(provider) === "s3" ? "S3" : "MinIO";
}

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

// Room Store markers are bound by the controller to Path(workspace).resolve(),
// which follows symlinks. Undefined when the real path cannot be resolved.
function realWorkspacePathSha256(workspace) {
  try {
    return crypto.createHash("sha256").update(fs.realpathSync(workspace)).digest("hex");
  } catch (_error) {
    return undefined;
  }
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
    const restoreArgs = nativeRegistry.dimensionArgs([
      "hydrate", selected.projectId,
      "--snapshot", selected.snapshotId,
      "--destination", stage,
      "--backend", provider,
      "--ide", "terminal",
    ], selected.dimensionId);
    await runSelectedEncryption(
      restoreArgs,
      cwd,
      selected.project?.dimension || { id: selected.dimensionId, provider },
      { title: `Restoring ${selected.label}…`, action: "switch a Room recovery point" },
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
  const message = sanitizeControllerText(error?.message || String(error));
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
  const platform = managedRuntime.resolvePlatform();
  const jatEnvironmentArtifact = managedRuntime.selectJatArtifact(manifest.jat, platform);
  let controllerEnvironmentArtifact;
  try {
    controllerEnvironmentArtifact = managedRuntime.selectControllerArtifact(manifest.controller, platform);
  } catch (error) {
    if (error.fallbackReason !== "controller-artifact-unpublished") throw error;
  }
  return {
    mode: "local-build-fallback",
    extension_version: manifest.extension_version,
    rcc_version: rcc.version,
    platform,
    jat_source_sha: manifest.jat.git_sha,
    ...(jatArtifactDigest ? { jat_artifact_digest: jatArtifactDigest } : {}),
    portable_jat_artifact_digest: jatEnvironmentArtifact.digest,
    controller_source_version: controllerSourceVersion(controllerRoot),
    controller_artifact_digest: controllerEnvironmentArtifact?.digest || "unpublished",
  };
}

async function localRuntimeState(context, manifest, rcc, jat, error, progressReporter, controllerRoot, cancellationToken) {
  if (cancellationToken?.isCancellationRequested) throw cancellationError();
  const options = runtimeProgressOptions(progressReporter, cancellationToken);
  progressReporter?.event({ stage: "runtime", message: `LOCAL BUILD FALLBACK: ${error.message}` });
  progressReporter?.event({ stage: "runtime", message: "Building controller environment locally; JAT remains lazy until a JAT operation" });
  const jatRoot = jat?.jatRoot;
  const identityBase = fallbackIdentity(manifest, rcc, controllerRoot);
  const warm = await managedRuntime.verifyLocalFallback(
    context, rcc, path.join(controllerRoot, "robot.yaml"), identityBase, options,
  );
  const marker = managedRuntime.readLocalFallbackRecord(context);
  const localJatDigest = (warm ? marker?.jat_artifact_digest : undefined) || jat?.artifact;
  const identity = {
    ...fallbackIdentity(manifest, rcc, controllerRoot, localJatDigest),
    ...(warm && marker?.jat_artifact_digest ? { local_jat_artifact_digest: marker.jat_artifact_digest } : {}),
  };
  let readyIdentity = identity;
  if (warm) {
    progressReporter?.event({ stage: "runtime", message: "Reusing verified LOCAL BUILD FALLBACK controller environment" });
  } else {
    if (await chooseLocalFallback(error) !== "build" || cancellationToken?.isCancellationRequested) throw cancellationError();
    await managedRuntime.prepareLocalController(
      context,
      rcc,
      path.join(controllerRoot, "robot.yaml"),
      options,
    );
    readyIdentity = fallbackIdentity(manifest, rcc, controllerRoot, jat?.artifact);
    await managedRuntime.writeLocalFallbackRecordWithLock(context, { ...readyIdentity, ...(jat?.artifact ? { local_jat_artifact_digest: jat.artifact } : {}) }, options);
    progressReporter?.event({
      stage: "runtime",
      message: "Controller environment ready (LOCAL BUILD FALLBACK); JAT remains lazy",
    });
  }
  const effectiveJatDigest = localJatDigest || jat?.artifact;
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

async function startRuntimeReadiness(context, progressReporter, cancellationToken) {
  if (cancellationToken?.isCancellationRequested) throw cancellationError();
  if (!managedRuntimePromise) {
    const preparation = { controller: createCancellationController(), consumers: new Set(), latest: undefined };
    runtimePreparation = preparation;
    const reporter = { event(event) {
      const safe = sanitizeProgressEvent(event);
      preparation.latest = safe;
      setRuntimeLifecycle("RCC_RESOLVING", safe.message);
      for (const consumer of preparation.consumers) {
        if (!consumer.token?.isCancellationRequested) consumer.reporter?.event(safe);
      }
    } };
    setRuntimeLifecycle("RCC_RESOLVING", "Resolving managed RCC");
    // Defer initialization until the first consumer is attached.
    managedRuntimePromise = Promise.resolve().then(() => runtimeReadinessOverride
      || initializeManagedRuntime(context, reporter, preparation.controller.token)).then((state) => {
      if (preparation.controller.token.isCancellationRequested) throw cancellationError();
      setRuntimeLifecycle("RUNTIME_READY", "Managed runtime ready; cached artifacts reused when available");
      return state || testRuntime;
    }).catch((error) => {
      const cancelled = isCancellationError(error);
      setRuntimeLifecycle(cancelled ? "CANCELLED" : "FAILED", cancelled
        ? "Runtime preparation cancelled. Select Retry to continue."
        : `Josh Room runtime unavailable: ${sanitizeControllerText(error.message)}`);
      managedRuntimePromise = undefined;
      throw error;
    }).finally(() => {
      preparation.controller.dispose();
      if (runtimePreparation === preparation) runtimePreparation = undefined;
    });
  }
  const preparation = runtimePreparation;
  const consumer = { token: cancellationToken, reporter: progressReporter };
  preparation?.consumers.add(consumer);
  if (preparation?.latest) progressReporter?.event(preparation.latest);
  else if (!preparation) progressReporter?.event({ stage: "runtime", message: "Reusing ready managed runtime" });
  const subscription = cancellationToken?.onCancellationRequested(() => {
    if (preparation && [...preparation.consumers].every((entry) => entry.token?.isCancellationRequested)) {
      preparation.controller.cancel();
    }
  });
  try {
    const state = await managedRuntimePromise;
    if (cancellationToken?.isCancellationRequested) throw cancellationError();
    return state;
  } finally {
    subscription?.dispose();
    preparation?.consumers.delete(consumer);
  }
}

function runtimeProgressOptions(progressReporter, cancellationToken) {
  let pending = "";
  let discardLine = false;
  return {
    cancellationToken,
    onProgress: (event) => progressReporter?.event(sanitizeProgressEvent({ stage: "runtime", message: event.message })),
    onOutput(stream, chunk) {
      // RCC stdout contains structured results and environment values.
      if (stream !== "stderr") return;
      let text = String(chunk);
      if (discardLine) {
        const newline = text.indexOf("\n");
        if (newline < 0) return;
        text = text.slice(newline + 1);
        discardLine = false;
      }
      const lines = (pending + text).split(/\r?\n/);
      pending = lines.pop() || "";
      if (pending.length > 4096) { pending = ""; discardLine = true; }
      for (const line of lines) {
        if (line.length > 4096 || /^\s*[\[{"}]/.test(line)) continue;
        const message = sanitizeRuntimeLine(line);
        if (!message) continue;
        outputChannel?.appendLine(`${new Date().toISOString()} RCC stderr: ${message}`);
        progressReporter?.event({ stage: "runtime", message });
      }
    },
  };
}

async function initializeManagedRuntime(context, progressReporter, cancellationToken) {
  const manifest = managedRuntime.readManifest();
  setStatus("$(sync~spin) Josh Room", "Preparing managed runtime…");
  progressReporter?.event({ stage: "runtime", message: "Resolving managed RCC" });
  const options = runtimeProgressOptions(progressReporter, cancellationToken);
  const rcc = await managedRuntime.ensureManagedRcc(context, manifest, options);
  const controllerRoot = controllerRootFor(context, manifest);
  progressReporter?.event({ stage: "runtime", message: "Preparing Josh Room controller environment" });
  let controller;
  try {
    controller = await managedRuntime.ensureControllerRuntime(context, manifest, rcc, options);
  } catch (error) {
    if (managedRuntime.localFallbackReason(error)) return localRuntimeState(context, manifest, rcc, undefined, error, progressReporter, controllerRoot, cancellationToken);
    throw error;
  }
  return {
    manifest,
    rcc,
    jatRcc: undefined,
    jat: undefined,
    controller,
    controllerRoot,
  };
}

function operationNeedsJat(args) {
  if (!Array.isArray(args) || !args.length) return false;
  if (["hydrate", "serve", "jat", "doctor", "enter"].includes(args[0])) return true;
  return args[0] === "snapshot" && ["create", "export", "serve", "extract"].includes(args[1]);
}

async function ensureJatForState(context, state, progressReporter, cancellationToken) {
  if (cancellationToken?.isCancellationRequested) throw cancellationError();
  if (!state.jatRcc) {
    state.jatRcc = state.manifest.jat?.rcc
      ? await managedRuntime.ensureManagedJatRcc(
        context,
        state.manifest,
        runtimeProgressOptions(progressReporter, cancellationToken),
      )
      : state.rcc;
  }
  if (state.mode === "local-build-fallback") {
    const jatRoot = await managedRuntime.ensureJatSource(
      context,
      state.manifest.jat,
      runtimeProgressOptions(progressReporter, cancellationToken),
    );
    state.jat = { ...state.jat, jatRoot };
    let identity;
    const localJat = await managedRuntime.buildLocalJatArtifact(
      context,
      state.jatRcc,
      path.join(jatRoot, "robot.yaml"),
      {
        ...runtimeProgressOptions(progressReporter, cancellationToken),
        expectedJatSourceSha: state.manifest.jat.git_sha,
        onArtifact: async (result) => {
          identity = {
            ...fallbackIdentity(state.manifest, state.rcc, state.controllerRoot, result.artifact),
            local_jat_artifact_digest: result.artifact,
          };
          await managedRuntime.writeLocalFallbackRecord(context, identity);
        },
      },
    );
    if (cancellationToken?.isCancellationRequested) throw cancellationError();
    state.localIdentity = identity;
    state.jat = { ...state.jat, artifact: localJat.artifact };
    if (cancellationToken?.isCancellationRequested) throw cancellationError();
    return state.jat;
  }
  if (state.jat?.artifact) {
    const jatRoot = await managedRuntime.ensureJatSource(
      context,
      state.manifest.jat,
      runtimeProgressOptions(progressReporter, cancellationToken),
    );
    try {
      await managedRuntime.verifyJatArtifact(
        context,
        state.manifest,
        state.jatRcc,
        state.jat.artifact,
        runtimeProgressOptions(progressReporter, cancellationToken),
      );
    } catch (error) {
      if (cancellationToken?.isCancellationRequested) throw cancellationError();
      const jat = await managedRuntime.ensureJatRuntime(
        context,
        state.manifest,
        state.jatRcc,
        { ...runtimeProgressOptions(progressReporter, cancellationToken), forceArchive: true },
      );
      state.jat = jat;
      return jat;
    }
    state.jat = { ...state.jat, jatRoot };
    return state.jat;
  }
  if (!managedJatPromise) {
    const preparation = { controller: createCancellationController(), consumers: new Set(), latest: undefined };
    jatPreparation = preparation;
    const reporter = { event(event) {
      preparation.latest = sanitizeProgressEvent(event);
      for (const consumer of preparation.consumers) {
        if (!consumer.token?.isCancellationRequested) consumer.reporter?.event(preparation.latest);
      }
    } };
    managedJatPromise = Promise.resolve().then(() => {
      reporter.event({ stage: "runtime", message: "Preparing JAT runtime for this operation" });
      return managedRuntime.ensureJatRuntime(context, state.manifest, state.jatRcc,
        runtimeProgressOptions(reporter, preparation.controller.token));
    }).then((jat) => {
      if (preparation.controller.token.isCancellationRequested) throw cancellationError();
      state.jat = jat;
      return jat;
    }).catch((error) => {
      managedJatPromise = undefined;
      throw error;
    }).finally(() => {
      preparation.controller.dispose();
      if (jatPreparation === preparation) jatPreparation = undefined;
    });
  }
  const preparation = jatPreparation;
  const consumer = { token: cancellationToken, reporter: progressReporter };
  preparation?.consumers.add(consumer);
  if (preparation?.latest) progressReporter?.event(preparation.latest);
  const subscription = cancellationToken?.onCancellationRequested(() => {
    if (preparation && [...preparation.consumers].every((entry) => entry.token?.isCancellationRequested)) {
      preparation.controller.cancel();
    }
  });
  try {
    const jat = await managedJatPromise;
    if (cancellationToken?.isCancellationRequested) throw cancellationError();
    return jat;
  } finally {
    subscription?.dispose();
    preparation?.consumers.delete(consumer);
  }
}

async function runtimeFor(cwd, args = [], progressReporter, cancellationToken) {
  if (cancellationToken?.isCancellationRequested) throw cancellationError();
  if (testRuntime) {
    setRuntimeLifecycle("RUNTIME_READY", "Managed runtime ready");
    return testRuntime;
  }
  if (!extensionContext) throw new Error("Josh Room extension runtime is not activated");
  const state = await startRuntimeReadiness(extensionContext, progressReporter, cancellationToken);
  if (operationNeedsJat(args)) await ensureJatForState(extensionContext, state, progressReporter, cancellationToken);
  if (cancellationToken?.isCancellationRequested) throw cancellationError();
  const localFallback = state.mode === "local-build-fallback";
  return {
    command: state.rcc.executable,
    args: localFallback
      ? (args) => ["run", "--silent", "--interactive", "-r", path.join(state.controllerRoot, "robot.yaml"), "-t", "Josh Room", "--", ...args, "--json"]
      : (args, receiptFile) => [
        "--no-build", "env", "exec", "--artifact", state.controller.artifact,
        "--permissive-local", "--receipt-file", receiptFile, "--inherit-streams", "--json",
        "--", "python", "-m", "josh_room", ...args, "--json",
      ],
    env: managedRuntime.runtimeEnvironment(extensionContext, {
      rccExecutable: state.rcc.executable,
      controllerRoot: state.controllerRoot,
      jatRoot: state.jat?.jatRoot,
      jatRccExecutable: state.jatRcc?.executable,
      jatArtifact: state.jat?.artifact,
      jatSourceSha: state.jat?.sourceSha,
      controllerArtifact: state.controller?.artifact,
    }, cwd),
    jatRoot: state.jat?.jatRoot,
    jatArtifact: state.jat?.artifact,
    controllerArtifact: state.controller?.artifact,
    mode: state.mode,
    markLocalReady: undefined,
  };
}

async function writeRuntimeCredentials(environment, { extensionMode = true, authFree = false } = {}) {
  environment.JOSH_ROOM_EXTENSION_MODE = extensionMode ? "1" : "0";
  if (authFree) {
    delete environment.JOSH_ROOM_PROVIDER_CREDENTIALS;
    return () => {};
  }
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

function installationRecoverySecretKey() {
  return INSTALLATION_RECOVERY_SECRET;
}

function readPrivateRecoveryFile(filename, label = "recovery identity") {
  const target = path.resolve(filename);
  const stat = fs.lstatSync(target);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16 * 1024 || (stat.mode & 0o077) !== 0) {
    throw new Error(`${label} must be a private regular file (owner-only permissions)`);
  }
  const value = fs.readFileSync(target, "utf8");
  if (!value.trim()) throw new Error(`${label} is empty`);
  return value.endsWith("\n") ? value : `${value}\n`;
}

function recoveryPathIsInsideWorkspace(workspace, target) {
  const absoluteWorkspace = path.resolve(workspace);
  const absoluteTarget = path.resolve(target);
  let resolvedWorkspace;
  let resolvedParent;
  try {
    resolvedWorkspace = fs.realpathSync.native(absoluteWorkspace);
    resolvedParent = fs.realpathSync.native(path.dirname(absoluteTarget));
  } catch (_error) {
    return true;
  }
  const relative = path.relative(resolvedWorkspace, path.join(resolvedParent, path.basename(absoluteTarget)));
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function writeRecoveryBackup(filename, value, workspace) {
  const target = path.resolve(filename);
  if (recoveryPathIsInsideWorkspace(workspace, target)) {
    throw new Error("recovery backup must be outside the workspace");
  }
  let descriptor;
  let created = false;
  try {
    descriptor = fs.openSync(target, "wx", 0o600);
    created = true;
    fs.writeFileSync(descriptor, value, "utf8");
    fs.fsyncSync(descriptor);
  } catch (error) {
    if (error?.code === "EEXIST") throw new Error("recovery backup already exists; choose a new destination");
    if (created) fs.rmSync(target, { force: true });
    throw error;
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
  fs.chmodSync(target, 0o600);
}

function materializeRecoveryValue(value) {
  if (typeof value !== "string" || !value.trim() || Buffer.byteLength(value) > 16 * 1024) {
    throw new Error("stored recovery identity is invalid");
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-recovery-"));
  fs.chmodSync(directory, 0o700);
  const filename = path.join(directory, "recovery.identity");
  try {
    fs.writeFileSync(filename, value.endsWith("\n") ? value : `${value}\n`, { mode: 0o600, flag: "wx" });
    fs.chmodSync(filename, 0o600);
  } catch (error) {
    fs.rmSync(directory, { recursive: true, force: true });
    throw error;
  }
  return {
    path: filename,
    value: readPrivateRecoveryFile(filename),
    cleanup: () => fs.rmSync(directory, { recursive: true, force: true }),
  };
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
  const recipients = material.recipients || material.recovery_recipients || material.recipient;
  if (recipients) {
    environment.JOSH_ROOM_SELECTED_RECIPIENTS = (Array.isArray(recipients) ? recipients : [recipients]).join(",");
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
    .map((line) => sanitizeRuntimeLine(line)
      .replace(/(?:\/home|\/private|\/Users|\/var\/home)\/[^\s,;)}\]]+/g, "[REDACTED PATH]")
      .replace(/[A-Za-z]:\\(?:[^\\\s]+\\)*[^\\\s,;)}\]]+/g, "[REDACTED PATH]"))
    .filter(Boolean)
    .join(" ")
    .slice(-CONTROLLER_DIAGNOSTIC_LIMIT);
}
function shellQuote(value) {
  return "'" + String(value).replace(/'/g, "'\\''") + "'";
}

function sanitizeControllerArgv(argv) {
  if (argv[0] === "composition") return ["composition", "plan", "[local selection redacted]"];
  const sanitized = [];
  const sensitiveFlag = /^-{1,2}(?:legacy-source-handoff|source-identity|recovery-handoff|material-handoff|access[-_ ]?key|secret[-_ ]?key|session[-_ ]?token|token|password|oauth[-_ ]?code|authorization|auth|identity|credential|credentials|secret|key|bearer)(?:$|[-_=])/i;
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

function controllerErrorText(value, depth = 0) {
  if (depth > 8) return "";
  if (typeof value === "string" || typeof value === "number") return sanitizeControllerText(value);
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  return [...new Set(["message", "error", "code", "error_code", "state"]
    .map((key) => controllerErrorText(value[key], depth + 1)).filter(Boolean))].join(" · ").slice(0, CONTROLLER_DIAGNOSTIC_LIMIT);
}

function redactResultDiagnostics(value, key = "", inDiagnostic = false) {
  if (/^argv$/i.test(key) && Array.isArray(value)) return sanitizeControllerArgv(value);
  const diagnosticContext = inDiagnostic || /error|message|diagnostic|stdout|stderr|workdir/i.test(key);
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
  const baseMessage = controllerErrorText(result?.error) || controllerErrorText(result?.message) || "Josh Room operation failed.";
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
    runtime = await runtimeFor(cwd, args, progressReporter, cancellationToken);
    if (cancellationToken?.isCancellationRequested) throw cancellationError();
    environment = managedRuntime.mergeRuntimeEnvironment(process.env, { ...(runtime.env || {}), JOSH_ROOM_PROGRESS_FILE: progressPath });
    credentialsCleanup = await writeRuntimeCredentials(environment, {
      extensionMode: runtime.mode !== "local-build-fallback" || Boolean(runtime.jatArtifact),
      authFree: ["status", "agents"].includes(args[0])
        || (args[0] === "composition" && ["plan", "inventory", "reconcile", "capture-plan"].includes(args[1]))
        || (options.localConfigurationOnly && args[0] === "dimensions" && !args.includes("--with-hierarchy")),
