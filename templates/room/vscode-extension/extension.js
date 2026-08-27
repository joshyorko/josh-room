const childProcess = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const vscode = require("vscode");
const { WorkspaceBaseline, isRoomMarker, shouldMarkDirty } = require("./dirty");
const {
  createProgressTracker,
  followProgressFile,
  formatProgressDisplay,
  operationKind,
} = require("./progress");
const { REGISTRY_URL, followLogFile, stageForLog, waitForRegistry } = require("./registry");

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

function setStatus(text, tooltip = "Open Josh Room") {
  if (!statusItem) return;
  statusItem.text = text;
  statusItem.tooltip = tooltip;
}

function activeWorkspace() {
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

function executeJoshRoom(args, cwd, cancellationToken, progressReporter) {
  return new Promise((resolve, reject) => {
    const progressDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-progress-"));
    fs.chmodSync(progressDirectory, 0o700);
    const progressPath = path.join(progressDirectory, "events.jsonl");
    fs.writeFileSync(progressPath, "", { mode: 0o600 });
    const followers = [followProgressFile(progressPath, (event) => progressReporter?.event(event))];
    if (["snapshot", "hydrate", "serve", "jat"].includes(args[0])) {
      const jatRoot = process.env.JOSH_ROOM_JAT_ROOT || path.join(os.homedir(), ".local", "share", "josh-room", "josh-all-the-things");
      const streamJat = (line, severity = "info") => {
        outputChannel?.[severity](line);
        const stage = stageForLog(line);
        if (stage) progressReporter?.event({ stage: "jat", message: stage });
      };
      followers.push(
        followLogFile(path.join(jatRoot, "output", "stdout.log"), (line) => streamJat(line)),
        followLogFile(path.join(jatRoot, "output", "stderr.log"), (line) => streamJat(line, "warn")),
      );
    }
    const cleanup = () => {
      for (const follower of followers) follower.dispose();
      fs.rmSync(progressDirectory, { recursive: true, force: true });
    };
    const child = childProcess.spawn("josh-room", [...args, "--json"], {
      cwd,
      env: { ...process.env, JOSH_ROOM_PROGRESS_FILE: progressPath },
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const append = (current, chunk) => (current + chunk.toString()).slice(-1024 * 1024);
    child.stdout.on("data", (chunk) => { stdout = append(stdout, chunk); });
    child.stderr.on("data", (chunk) => { stderr = append(stderr, chunk); });
    const cancellation = cancellationToken?.onCancellationRequested(() => {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch (_error) {
        child.kill("SIGTERM");
      }
    });
    child.on("error", (error) => {
      cancellation?.dispose();
      cleanup();
      reject(error);
    });
    child.on("close", (code) => {
      cancellation?.dispose();
      cleanup();
      if (code === 0) {
        resolve({ stdout, stderr });
      } else {
        const error = new Error(`Josh Room exited with status ${code}`);
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
      }
    });
  });
}

async function runJoshRoom(args, cwd, cancellationToken, progressReporter) {
  outputChannel?.info(`START · josh-room ${args.join(" ")}`);
  try {
    const { stdout } = await executeJoshRoom(args, cwd, cancellationToken, progressReporter);
    const result = JSON.parse(stdout);
    // `status` deliberately returns ok=false for a changed or unlinked
    // workspace. That is authoritative state, not an operation failure.
    if (!result.ok && args[0] !== "status") {
      throw new Error(result.error || "Josh Room operation failed.");
    }
    outputChannel?.info(`DONE · ${result.project_id || result.operation || args[0]}`);
    return result;
  } catch (error) {
    const output = error.stdout || error.stderr;
    if (output) {
      try {
        const result = JSON.parse(output);
        if (args[0] === "status") {
          outputChannel?.info(`DONE · status (${result.state || "unknown"})`);
          return result;
        }
        throw new Error(result.error || "Josh Room operation failed.");
      } catch (parseError) {
        if (parseError instanceof SyntaxError) {
          outputChannel?.error(error.message || String(error));
          outputChannel?.show(true);
          throw error;
        }
        throw parseError;
      }
    }
    outputChannel?.error(error.message || String(error));
    outputChannel?.show(true);
    throw error;
  }
}

function runOperation(title, args, cwd, { cancellable = true } = {}) {
  const operationId = ++activeOperationId;
  const displayTitle = title.replace(/…$/, "");
  return vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title, cancellable },
    async (progress, token) => {
      const reporter = createVisualReporter(displayTitle, operationKind(args), progress, operationId);
      try {
        const result = await runJoshRoom(args, cwd, token, reporter);
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
  let animation;
  const updateStatus = () => {
    if (!latestState) return;
    const animated = formatProgressDisplay(title, kind, latestState, frame);
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
    const state = tracker.update(event);
    latestState = state;
    const display = formatProgressDisplay(title, kind, state);
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
      const message = error?.message || String(error);
      outputChannel?.error(`FAILED · ${title} · ${message}`);
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
      { label: "Pack Folder into Haul", description: "Build", icon: "package", command: "joshRoom.jatBuild" },
      { label: "Restore JAT Haul", description: "Restore", icon: "folder-library", command: "joshRoom.jatRestore" },
      { label: "Serve Hauler Haul", description: "Registry :5000", icon: "server-process", command: "joshRoom.jatServe" },
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
  if (targetDimension
    && nativeRegistry.providerKey(targetDimension.provider) === "r2"
    && catalog.auth_state
    && catalog.auth_state !== "connected") {
    await connectCloudflare({ dimension: targetDimension });
  }
  const imageChoice = await vscode.window.showQuickPick([
    { label: "Workspace only", allImages: false },
    { label: "Workspace + all tagged local OCI images", allImages: true },
  ], { title: "Include local OCI images?", ignoreFocusOut: true });
  if (!imageChoice) return "cancelled";
  const buildArgs = nativeRegistry.dimensionArgs(
    ["snapshot", "create", name, "--source", source, "--backend", "r2"],
    targetDimensionId || selectedDimensionId,
  );
  if (imageChoice.allImages) buildArgs.push("--all-images");
  const result = await runOperation(`Saving ${name}…`, buildArgs, source);
  const size = (result.ciphertext_size / (1024 * 1024)).toFixed(1);
  await vscode.window.showInformationMessage(`Saved “${name}” (${size} MiB).`);
  if (path.resolve(source) === path.resolve(cwd)) {
    await startDirtyTracking(extensionContext);
  }
  await roomsProvider?.refresh();
  return "saved";
}

async function enterRoom(preferredProject) {
  const cwd = activeWorkspace();
  const catalog = await loadCatalog(cwd);
  const projects = nativeRegistry.flattenDimensionRooms(catalog);
  const preferred = roomProject(preferredProject);
  const preferredSnapshotId = snapshotIdentity(preferredProject);
  const selected = preferred
    ? { label: roomLabel(preferred), projectId: preferred.id, project: preferred, snapshotId: preferredSnapshotId }
    : await vscode.window.showQuickPick(
      projects.map((project) => ({ label: roomLabel(project), projectId: project.id, project })),
      { title: "Josh: Enter Room", placeHolder: "What do you want to work on?", ignoreFocusOut: true },
  );
  if (!selected) return "cancelled";
  if (sameRoomBinding(currentRoom(cwd), selected)) {
    await vscode.window.showInformationMessage(`“${selected.label}” is already open.`);
    return "current";
  }
  const history = await runOperation(
    `Loading ${selected.label} recovery points…`,
    nativeRegistry.dimensionArgs(["snapshots", "list", selected.projectId, "--backend", "r2"], dimensionId(selected.project)),
    cwd,
  );
  let snapshotId = selected.snapshotId || "latest";
  if (!selected.snapshotId && history.snapshots.length > 1) {
    const snapshot = await vscode.window.showQuickPick(
      history.snapshots
        .map((item) => ({
          label: item.snapshot_id === history.latest ? "Latest snapshot" : "Previous snapshot",
          description: item.created_at || item.snapshot_id,
          snapshotId: item.snapshot_id,
        }))
        .sort((left) => left.snapshotId === history.latest ? -1 : 1),
      { title: `Josh: Enter ${selected.label}`, placeHolder: "Choose a recovery point", ignoreFocusOut: true },
    );
    if (!snapshot) return "cancelled";
    snapshotId = snapshot.snapshotId;
  }
  const root = roomsRoot(cwd);
  const destination = path.join(root, selected.projectId);
  if (sameRoomBinding(currentRoom(destination), selected)) {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Opening existing ${selected.label}…`, cancellable: false },
      () => vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false),
    );
    return "opened";
  }
  if (fs.existsSync(destination)) {
    throw new Error(`Refusing to replace an unexplained existing folder: ${destination}`);
  }
  const hydrateDimensionId = dimensionId(selected.project) || selectedDimensionId;
  await runOperation(
    `Restoring ${selected.label}…`,
    nativeRegistry.dimensionArgs([
      "hydrate",
      selected.projectId,
      "--snapshot",
      snapshotId,
      "--destination",
      destination,
      "--backend",
      "r2",
      "--ide",
      "terminal",
    ], hydrateDimensionId),
    cwd,
  );
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

async function serveRoom(preferredProject) {
  const cwd = activeWorkspace();
  const marker = currentRoom(cwd);
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  const projects = nativeRegistry.flattenDimensionRooms(catalog);
  const project = await chooseServeProject(projects, marker, preferredProject);
  if (!project) return "cancelled";
  const history = await runOperation(
    `Loading ${project.display_name} recovery points…`,
    nativeRegistry.dimensionArgs(["snapshots", "list", project.id, "--backend", "r2"], dimensionId(project)),
    cwd,
  );
  const preferredSnapshotId = snapshotIdentity(preferredProject);
  const markerSnapshotId = marker && marker.format_version === 2 && marker.snapshot_id;
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
  if (!/^[a-z0-9-]+$/.test(project.id) || !/^[a-z0-9-]+$/.test(snapshotId)) {
    throw new Error("Room or snapshot identity is unsafe for terminal execution.");
  }
  return startRegistryTerminal({
    cwd,
    title: `Serving ${project.display_name}`,
    terminalName: `Images: ${project.display_name}`,
    command: `josh-room serve ${project.id} --snapshot ${snapshotId} --backend r2 --dimension ${dimensionId(project) || selectedDimensionId}`,
    retry: () => vscode.commands.executeCommand("joshRoom.serve", project),
  });
}

function shellQuote(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

async function jatBuild() {
  const cwd = activeWorkspace();
  const folders = await vscode.window.showOpenDialog({
    title: "JAT: Pack Folder into Haul",
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
  const args = ["jat", "build", "--source", source, "--output", output.fsPath];
  if (imageChoice.allImages) args.push("--all-images");
  const result = await runOperation(`Packing ${path.basename(source)}…`, args, cwd);
  await vscode.window.showInformationMessage(`Created ${result.payload_path || output.fsPath}.`);
  return "built";
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
    validateInput: (value) => value.trim() && !value.includes("/") ? undefined : "Enter one folder name.",
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

async function jatServe() {
  const cwd = activeWorkspace();
  const files = await vscode.window.showOpenDialog({
    title: "JAT: Serve Hauler Haul",
    defaultUri: vscode.Uri.file(cwd),
    canSelectFiles: true,
    canSelectFolders: false,
    canSelectMany: false,
    filters: { "Hauler archive": ["zst"] },
    openLabel: "Serve this haul",
  });
  if (!files?.length) return "cancelled";
  const haul = files[0].fsPath;
  return startRegistryTerminal({
    cwd,
    title: `Serving ${path.basename(haul)}`,
    terminalName: "JAT Hauler Registry",
    command: `josh-room jat serve --haul ${shellQuote(haul)}`,
    retry: () => vscode.commands.executeCommand("joshRoom.jatServe"),
  });
}

async function startRegistryTerminal({ cwd, title, terminalName, command, retry }) {
  const jatRoot = process.env.JOSH_ROOM_JAT_ROOT || path.join(os.homedir(), ".local", "share", "josh-room", "josh-all-the-things");
  const progressDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-registry-"));
  fs.chmodSync(progressDirectory, 0o700);
  const progressPath = path.join(progressDirectory, "events.jsonl");
  fs.writeFileSync(progressPath, "", { mode: 0o600 });
  const terminal = vscode.window.createTerminal({ name: terminalName, cwd });
  const followers = [];
  let latestLog = "";
  let progressReporter;
  let progressActive = false;
  const stream = (line, severity = "info") => {
    latestLog = line;
    outputChannel?.[severity](line);
    const stage = stageForLog(line);
    if (stage && progressActive) progressReporter?.event({ stage: "jat", message: stage });
  };
  followers.push(
    followProgressFile(progressPath, (event) => {
      latestLog = event.message;
      if (progressActive) progressReporter?.event(event);
    }),
    followLogFile(path.join(jatRoot, "output", "stdout.log"), (line) => stream(line)),
    followLogFile(path.join(jatRoot, "output", "stderr.log"), (line) => stream(line, "warn")),
  );
  const stopFollowing = () => {
    for (const follower of followers) follower.dispose();
    progressReporter?.dispose();
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
  setStatus("$(sync~spin) Starting registry", title);
  terminal.show(true);
  try {
    const catalog = await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title,
        cancellable: false,
      },
      async (progress) => {
        progressReporter = createVisualReporter(title, "serve", progress);
        progressActive = true;
        progressReporter.event({ stage: "auth", message: "Preparing secure Room session" });
        terminal.sendText(`JOSH_ROOM_PROGRESS_FILE=${shellQuote(progressPath)} ${command}`, true);
        try {
          return await waitForRegistry();
        } finally {
          progressActive = false;
        }
      },
    );
    const repositories = Array.isArray(catalog.repositories) ? catalog.repositories : [];
    const count = repositories.length;
    progressReporter?.event({ stage: "complete", message: "Registry ready" });
    setStatus("$(server-process) Registry :5000", `${count} repositories · ${title}`);
    outputChannel?.info(`READY · ${REGISTRY_URL} · ${count} ${count === 1 ? "repository" : "repositories"}`);
    const action = await vscode.window.showInformationMessage(
      `Hauler registry is ready — ${count} ${count === 1 ? "repository" : "repositories"} on ${REGISTRY_URL}.`,
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
    progressActive = false;
    const detail = latestLog || error.message || String(error);
    progressReporter?.fail(error);
    outputChannel?.error(`Registry failed: ${detail}`);
    setStatus("$(error) Registry failed", detail);
    const action = await vscode.window.showErrorMessage(
      `Hauler registry failed to start: ${detail.slice(0, 240)}`,
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
  register(context, "joshRoom.jatBuild", jatBuild);
  register(context, "joshRoom.jatRestore", jatRestore);
  register(context, "joshRoom.jatServe", jatServe);
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
  startDirtyTracking,
};

const nativeRegistry = require("./registry");
let selectedDimensionId;

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

async function connectCloudflare(item, { pollIntervalMs = 2000, timeoutMs = 600000 } = {}) {
  const cwd = activeWorkspace();
  let dimension = item && item.dimension ? item.dimension : item;
  if (!dimension) {
    const catalog = await loadCatalog(cwd, "Loading storage...");
    dimension = dimensionList(catalog).find((candidate) => nativeRegistry.providerKey(candidate.provider) === "r2");
  }
  const targetDimensionId = dimension && (dimension.id || dimension.dimension_id) || "r2";
  selectedDimensionId = targetDimensionId;
  const started = await runOperation(
    "Connecting Cloudflare...",
    ["auth", "start", "--dimension", targetDimensionId],
    cwd,
    { cancellable: false },
  );
  const authorizationUrl = started.authorization_url || started.authorizationUrl;
  const sessionId = started.session_id || started.sessionId;
  if (!authorizationUrl || !sessionId) throw new Error("Cloudflare connection did not return an authorization session.");
  const opened = await vscode.env.openExternal(vscode.Uri.parse(authorizationUrl));
  if (opened === false) throw new Error("Could not open Cloudflare authorization in your local browser.");

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await runOperation(
      "Waiting for Cloudflare approval...",
      ["auth", "poll", sessionId, "--dimension", targetDimensionId],
      cwd,
      { cancellable: false },
    );
    if (result.status === "authorized") {
      if (roomsProvider) await roomsProvider.refresh();
      await vscode.window.showInformationMessage("Cloudflare connected. Your Rooms are ready.");
      return "connected";
    }
    if (result.status !== "pending") throw new Error(`Cloudflare authorization ${result.status || "failed"}`);
    if (pollIntervalMs > 0) await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }
  throw new Error("Cloudflare authorization timed out");
}

async function addStorage() {
  const cwd = activeWorkspace();
  const providerChoice = await vscode.window.showQuickPick([
    { label: "Cloudflare R2", provider: "r2" },
    { label: "MinIO", provider: "minio" },
  ], { title: "Josh: Add Storage", placeHolder: "Choose a storage provider", ignoreFocusOut: true });
  if (!providerChoice) return "cancelled";
  if (providerChoice.provider === "r2") return connectCloudflare();

  const name = await vscode.window.showInputBox({
    title: "Josh: Add Storage",
    prompt: "Friendly Dimension name",
    ignoreFocusOut: true,
    validateInput: (value) => value.trim() ? undefined : "Enter a Dimension name.",
  });
  if (!name) return "cancelled";
  const endpoint = await vscode.window.showInputBox({
    title: "Josh: Add MinIO Storage", prompt: "Endpoint URL", placeHolder: "https://...", ignoreFocusOut: true,
    validateInput: (value) => value.trim().startsWith("http://")
      || value.trim().startsWith("https://") ? undefined : "Enter an http(s) endpoint.",
  });
  if (!endpoint) return "cancelled";
  const bucket = await vscode.window.showInputBox({
    title: "Josh: Add MinIO Storage", prompt: "Bucket or object-store namespace", ignoreFocusOut: true,
    validateInput: (value) => value.trim() ? undefined : "Enter a bucket.",
  });
  if (!bucket) return "cancelled";
  const profile = await vscode.window.showInputBox({
    title: "Josh: Add MinIO Storage", prompt: "Existing host keyring credential profile (name only)", ignoreFocusOut: true,
    validateInput: (value) => value.trim() ? undefined : "Enter the keyring profile name.",
  });
  if (!profile) return "cancelled";
  const id = name.trim().toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
  if (!id) throw new Error("Dimension name must contain letters or numbers.");
  await runOperation("Adding " + name.trim() + "...", [
    "dimensions", "add", id, "--display-name", name.trim(), "--provider", providerChoice.provider,
    "--endpoint", endpoint.trim(), "--bucket", bucket.trim(),
    "--credential-profile", profile.trim(),
  ], cwd);
  selectedDimensionId = id;
  if (roomsProvider) await roomsProvider.refresh();
  await vscode.window.showInformationMessage(
    "Added MinIO storage " + name.trim() + ". Credentials remain in the host keyring.",
  );
  return "added";
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
    const emptyKind = ["load", "loading", "empty", "error"].includes(item.kind);
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
      ? item.state === "expired" ? "r2-connection-expired"
        : item.state === "connected" ? "r2-connection-connected" : "r2-connection"
      : syntheticDimension ? "dimension-synthetic" : item.kind;
    treeItem.description = item.description || "";
    treeItem.iconPath = new vscode.ThemeIcon(
      item.kind === "provider" ? "cloud"
        : item.kind === "dimension" ? "database"
          : item.kind === "connection" ? item.state === "connected" ? "pass-filled" : item.state === "expired" ? "warning" : "plug"
            : item.kind === "room" ? "archive" : "history",
    );
    if (item.kind === "dimension") {
      treeItem.command = { command: "joshRoom.selectDimension", title: "Select Storage", arguments: [item] };
    }
    if (item.kind === "connection" && item.state !== "connected") {
      const command = item.state === "expired" ? "joshRoom.reconnectCloudflare" : "joshRoom.connectCloudflare";
      const title = item.state === "expired" ? "Reconnect Cloudflare" : "Connect Cloudflare";
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
    if (this.state === "loading") return [{ kind: "loading" }];
    if (this.state === "error") return [{ kind: "error" }];
    if (this.state === "initial") return [{ kind: "load" }];
    return this.roots && this.roots.length ? this.roots : [{ kind: "empty" }];
  }

  async refresh() {
    this.state = "loading";
    this.emitter.fire(undefined);
    try {
      const catalog = await loadCatalog(activeWorkspace(), "Refreshing Dimensions and Rooms...");
      this.roots = nativeRegistry.buildProviderTree(catalog);
      this.state = "ready";
      await vscode.commands.executeCommand("setContext", "joshRoom.roomsEmpty", this.roots.length === 0);
      refreshRoomStatus();
      this.emitter.fire(undefined);
      return catalog;
    } catch (error) {
      this.state = "error";
      this.emitter.fire(undefined);
      throw error;
    }
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
    const catalogSnapshotIds = catalogProject && (catalogProject.snapshots || [])
      .map((snapshot) => snapshot.snapshot_id || snapshot.id);
    const snapshotId = preferredSnapshotId || latestSnapshotId(catalogProject);
    if (catalogProject && snapshotId && (!preferredSnapshotId || catalogSnapshotIds.includes(snapshotId))) {
      return { ...catalogProject, project_id: catalogProject.id, snapshot_id: snapshotId };
    }
  }
  const choices = projects.flatMap((project) => {
    const snapshots = Array.isArray(project.snapshots) ? project.snapshots : [];
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
  const snapshots = Array.isArray(project.snapshots) ? project.snapshots : [];
  return snapshots[0] && (snapshots[0].snapshot_id || snapshots[0].id);
}

async function linkRoom(preferredProject) {
  const cwd = activeWorkspace();
  const context = await explicitRoomContext(preferredProject, "Josh: Link Room");
  if (!context) return "cancelled";
  const args = ["link", "--project", context.project_id, "--snapshot", context.snapshot_id];
  const result = await runOperation("Linking saved Room...", routeItemArgs(args, context), cwd);
  await resetNativeBaseline(result);
  if (roomsProvider) await roomsProvider.refresh();
  return "linked";
}

async function repairRoom(preferredProject) {
  const cwd = activeWorkspace();
  const context = await explicitRoomContext(preferredProject, "Josh: Repair Room Ledger");
  if (!context) return "cancelled";
  const args = ["repair", "--project", context.project_id, "--snapshot", context.snapshot_id];
  const result = await runOperation("Repairing Room ledger...", routeItemArgs(args, context), cwd);
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
  register(context, "joshRoom.selectDimension", selectDimension);
  register(context, "joshRoom.editStorageSettings", editStorageSettings);
  register(context, "joshRoom.enter", enterRoom);
  register(context, "joshRoom.link", linkRoom);
  register(context, "joshRoom.repair", repairRoom);
  register(context, "joshRoom.remove", removeRoom);
  register(context, "joshRoom.serve", serveRoom);
  register(context, "joshRoom.refresh", () => roomsProvider.refresh());
  register(context, "joshRoom.jatBuild", jatBuild);
  register(context, "joshRoom.jatRestore", jatRestore);
  register(context, "joshRoom.jatServe", jatServe);
  startDirtyTracking(context).catch((error) => {
    outputChannel.warn("Unable to index saved Room: " + error.message);
    setRoomDirty(true);
  });
  roomsProvider.refresh().catch((error) => outputChannel.appendLine("error: " + error.message));
}

module.exports.activate = activateNative;

async function loadNativeCatalogWithSnapshots(cwd, title) {
  const catalog = await runOperation(title || "Loading your Rooms...", ["dimensions", "list"], cwd);
  const authState = await runOperation(
    title || "Loading your Rooms...",
    ["auth", "status"],
    cwd,
    { cancellable: false },
  ).catch((error) => {
    outputChannel && outputChannel.warn("Cloudflare connection state unavailable: " + error.message);
    return { state: "missing" };
  });
  const dimensions = dimensionList(catalog);
  const hasR2 = dimensions.some((dimension) => nativeRegistry.providerKey(dimension.provider) === "r2");
  const legacyCatalog = !dimensions.length && (catalog.dimension_id || Array.isArray(catalog.projects));
  if (!hasR2 && !legacyCatalog) {
    const r2 = {
      id: "r2",
      display_name: "Default",
      provider: "r2",
      synthetic: true,
      connection_state: authState.state || "missing",
      projects: [],
    };
    if (r2.connection_state === "connected") {
      try {
        const listed = await runOperation(title || "Loading your Rooms...", ["projects", "list", "--backend", "r2"], cwd);
        r2.id = listed.dimension_id || r2.id;
        r2.projects = listed.projects || [];
      } catch (error) {
        outputChannel && outputChannel.warn("Unable to load Rooms for Cloudflare R2: " + error.message);
      }
    }
    dimensions.push(r2);
  }
  for (const dimension of dimensions) {
    const id = dimension.id || dimension.dimension_id;
    if (!id) {
      outputChannel && outputChannel.warn("Skipping a Dimension without dimension_id.");
      dimension.projects = [];
      continue;
    }
    const provider = nativeRegistry.providerKey(dimension.provider);
    if (provider === "r2") {
      dimension.connection_state = authState.state || dimension.connection_state || "connected";
      if (dimension.connection_state !== "connected") {
        dimension.projects = [];
        continue;
      }
    }
    let projects = dimension.projects || dimension.rooms;
    try {
      if (projects && !Array.isArray(projects)) {
        projects = Object.entries(projects).map(([projectId, value]) =>
          Object.assign({}, value || {}, { id: (value && (value.id || value.project_id)) || projectId }));
      }
      if (!Array.isArray(projects)) {
        const listed = await runOperation(title || "Loading your Rooms...",
          nativeRegistry.dimensionArgs(["projects", "list"], id), cwd);
        if (listed.dimension_id && listed.dimension_id !== id) {
          throw new Error("Dimension response mismatch: expected " + id + ", received " + listed.dimension_id + ".");
        }
        projects = listed.projects || [];
      }
    } catch (error) {
      outputChannel && outputChannel.warn("Unable to load Rooms for Dimension " + id + ": " + error.message);
      projects = [];
    }
    dimension.projects = Array.isArray(projects) ? projects : [];
    for (const project of dimension.projects) {
      const projectId = project.id || project.project_id;
      if (!projectId) continue;
      try {
        const listed = await runOperation(title || "Loading your Rooms...",
          nativeRegistry.dimensionArgs(["snapshots", "list", projectId], id), cwd);
        if (listed.dimension_id && listed.dimension_id !== id) {
          throw new Error("Dimension response mismatch: expected " + id + ", received " + listed.dimension_id + ".");
        }
        project.snapshots = listed.snapshots || [];
        project.latest = listed.latest;
      } catch (error) {
        outputChannel && outputChannel.warn("Unable to load JATs for Room " + projectId + " in Dimension " + id + ": " + error.message);
        project.snapshots = Array.isArray(project.snapshots) ? project.snapshots : [];
      }
    }
  }
  return Object.assign({}, catalog, {
    dimensions,
    auth_state: authState.state,
    projects: nativeRegistry.flattenDimensionRooms({ ...catalog, dimensions }),
  });
}

loadNativeCatalog = loadNativeCatalogWithSnapshots;
loadCatalog = loadNativeCatalogWithSnapshots;

async function editStorageSettings(item) {
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
  selectedDimensionId = dimension.id || dimension.dimension_id;
  const displayName = await vscode.window.showInputBox({
    title: "Edit Settings", prompt: "Storage name", value: dimension.display_name || dimension.name || "",
  });
  if (!displayName) return "cancelled";
  const args = [
    "dimensions", "update", selectedDimensionId,
    "--display-name", displayName.trim(),
  ];
  if (nativeRegistry.providerKey(dimension.provider) === "minio") {
    const endpoint = await vscode.window.showInputBox({
      title: "Edit MinIO Settings", prompt: "Endpoint URL", value: dimension.endpoint || "",
    });
    if (!endpoint) return "cancelled";
    const bucket = await vscode.window.showInputBox({
      title: "Edit MinIO Settings", prompt: "Bucket", value: dimension.bucket || "",
    });
    if (!bucket) return "cancelled";
    const region = await vscode.window.showInputBox({
      title: "Edit MinIO Settings", prompt: "Region", value: dimension.region || "",
    });
    if (!region) return "cancelled";
    const profile = await vscode.window.showInputBox({
      title: "Edit MinIO Settings", prompt: "Existing host keyring credential profile (name only)",
      value: dimension.credential_profile || dimension.credentialProfile || "",
    });
    if (!profile) return "cancelled";
    args.push(
      "--endpoint", endpoint.trim(), "--bucket", bucket.trim(), "--region", region.trim(),
      "--credential-profile", profile.trim(),
    );
  }
  await runOperation("Updating storage...", args, activeWorkspace());
  if (roomsProvider) await roomsProvider.refresh();
  return "updated";
}

Object.assign(module.exports.__test__, {
  chooseServeProject,
  addStorage,
  connectCloudflare,
  editStorageSettings,
  HierarchyRoomsProvider,
  linkRoom,
  loadCatalog,
  repairRoom,
  removeSnapshot,
  RoomDragAndDropController,
  roomLabel,
  runJoshRoom,
  selectDimension,
  setRoomsProvider(value) {
    roomsProvider = value;
  },
});
