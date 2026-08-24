const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");
const util = require("util");
const vscode = require("vscode");

const execFile = util.promisify(childProcess.execFile);
let outputChannel;
let roomsProvider;

function activeWorkspace() {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) {
    throw new Error("Open a workspace folder before using Josh Room.");
  }
  return folder.uri.fsPath;
}

async function runJoshRoom(args, cwd) {
  outputChannel?.appendLine(`> josh-room ${args.join(" ")}`);
  try {
    const { stdout } = await execFile("josh-room", [...args, "--json"], {
      cwd,
      env: process.env,
      maxBuffer: 1024 * 1024,
    });
    const result = JSON.parse(stdout);
    if (!result.ok) {
      throw new Error(result.error || "Josh Room operation failed.");
    }
    outputChannel?.appendLine(`ok: ${result.project_id || result.operation || args[0]}`);
    return result;
  } catch (error) {
    const output = error.stdout || error.stderr;
    if (output) {
      try {
        const result = JSON.parse(output);
        throw new Error(result.error || "Josh Room operation failed.");
      } catch (parseError) {
        if (parseError instanceof SyntaxError) {
          outputChannel?.appendLine(`error: ${error.message || String(error)}`);
          outputChannel?.show(true);
          throw error;
        }
        throw parseError;
      }
    }
    outputChannel?.appendLine(`error: ${error.message || String(error)}`);
    outputChannel?.show(true);
    throw error;
  }
}

class RoomsProvider {
  constructor() {
    this.rooms = undefined;
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  getTreeItem(item) {
    if (item.kind === "load") {
      const treeItem = new vscode.TreeItem("Load Rooms", vscode.TreeItemCollapsibleState.None);
      treeItem.iconPath = new vscode.ThemeIcon("cloud-download");
      treeItem.command = { command: "joshRoom.refresh", title: "Load Rooms" };
      return treeItem;
    }
    const current = currentRoom(activeWorkspace())?.project_id === item.id;
    const treeItem = new vscode.TreeItem(item.display_name, vscode.TreeItemCollapsibleState.None);
    treeItem.description = current ? "Current" : "";
    treeItem.tooltip = current ? `${item.display_name} — current workspace` : item.display_name;
    treeItem.contextValue = "room";
    treeItem.iconPath = new vscode.ThemeIcon(current ? "home" : "archive");
    treeItem.command = { command: "joshRoom.enter", title: "Enter Room", arguments: [item] };
    return treeItem;
  }

  getChildren() {
    return this.rooms === undefined ? [{ kind: "load" }] : this.rooms;
  }

  async refresh() {
    const catalog = await loadCatalog(activeWorkspace(), "Refreshing Rooms…");
    this.rooms = catalog.projects;
    this.emitter.fire(undefined);
  }
}

async function loadCatalog(cwd, title = "Loading your Rooms…") {
  return vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title, cancellable: false },
    () => runJoshRoom(["projects", "list", "--backend", "r2"], cwd),
  );
}

function currentRoom(cwd) {
  try {
    const marker = JSON.parse(fs.readFileSync(path.join(cwd, ".josh-room.json"), "utf8"));
    return marker.format_version === 1 ? marker : undefined;
  } catch (_error) {
    return undefined;
  }
}

async function saveRoom() {
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
  const marker = currentRoom(source);
  const selected = await vscode.window.showQuickPick([
    { label: "$(add) Create a new Room…", create: true },
    ...catalog.projects.map((project) => ({
      label: project.display_name,
      description: marker?.project_id === project.id ? "Current Room" : "Save a new latest snapshot",
      project,
    })),
  ], { title: "Josh: Save Room", placeHolder: "Create or update a Room", ignoreFocusOut: true });
  if (!selected) return "cancelled";
  let name = selected.project?.display_name;
  if (selected.create) {
    name = await vscode.window.showInputBox({
      title: "Josh: Save Room",
      prompt: "Name this Room",
      ignoreFocusOut: true,
      validateInput: (value) => value.trim() ? undefined : "Enter a Room name.",
    });
  }
  if (!name) return "cancelled";
  if (selected.project) {
    const existing = path.join(path.dirname(cwd), selected.project.id);
    if (existing !== source && currentRoom(existing)?.project_id === selected.project.id) {
      const action = await vscode.window.showInformationMessage(
        `“${selected.project.display_name}” already has a working folder.`,
        "Open Room",
      );
      if (action === "Open Room") {
        await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(existing), false);
      }
      return "opened";
    }
    if (marker?.project_id !== selected.project.id) {
      const confirmed = await vscode.window.showWarningMessage(
        `Replace the latest “${selected.project.display_name}” snapshot with the contents of ${source}? The previous snapshot remains recoverable.`,
        { modal: true },
        "Replace Latest",
      );
      if (confirmed !== "Replace Latest") return "cancelled";
    }
  }
  const imageChoice = await vscode.window.showQuickPick([
    { label: "Workspace only", allImages: false },
    { label: "Workspace + all tagged local OCI images", allImages: true },
  ], { title: "Include local OCI images?", ignoreFocusOut: true });
  if (!imageChoice) return "cancelled";
  const buildArgs = ["snapshot", "create", name, "--source", source, "--backend", "r2"];
  if (imageChoice.allImages) buildArgs.push("--all-images");
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Saving ${name}…`, cancellable: false },
    () => runJoshRoom(buildArgs, source),
  );
  const size = (result.ciphertext_size / (1024 * 1024)).toFixed(1);
  await vscode.window.showInformationMessage(`Saved “${name}” (${size} MiB).`);
  await roomsProvider?.refresh();
  return "saved";
}

async function enterRoom(preferredProject) {
  const cwd = activeWorkspace();
  const catalog = await loadCatalog(cwd);
  const selected = preferredProject
    ? { label: preferredProject.display_name, projectId: preferredProject.id }
    : await vscode.window.showQuickPick(
      catalog.projects.map((project) => ({ label: project.display_name, projectId: project.id })),
      { title: "Josh: Enter Room", placeHolder: "What do you want to work on?", ignoreFocusOut: true },
    );
  if (!selected) return "cancelled";
  const history = await runJoshRoom(["snapshots", "list", selected.projectId, "--backend", "r2"], cwd);
  let snapshotId = "latest";
  if (history.snapshots.length > 1) {
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
  const root = path.basename(cwd) === "room" ? path.dirname(cwd) : cwd;
  const destination = path.join(root, selected.projectId);
  if (currentRoom(destination)?.project_id === selected.projectId) {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Opening existing ${selected.label}…`, cancellable: false },
      () => vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false),
    );
    return "opened";
  }
  if (fs.existsSync(destination)) {
    throw new Error(`Refusing to replace an unexplained existing folder: ${destination}`);
  }
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Restoring ${selected.label}…`, cancellable: false },
    () => runJoshRoom([
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
    ], cwd),
  );
  await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false);
  return "opened";
}

async function removeRoom(preferredProject) {
  const cwd = activeWorkspace();
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  const selected = preferredProject
    ? { label: preferredProject.display_name, project: preferredProject }
    : await vscode.window.showQuickPick(
      catalog.projects.map((project) => ({ label: project.display_name, project })),
      { title: "Josh: Remove Room", placeHolder: "Choose a Room to remove", ignoreFocusOut: true },
    );
  if (!selected) return "cancelled";
  const snapshots = await runJoshRoom(["snapshots", "list", selected.project.id, "--backend", "r2"], cwd);
  const confirmed = await vscode.window.showWarningMessage(
    `Remove “${selected.label}” and its ${snapshots.snapshots.length} snapshot(s)? This cannot be undone.`,
    { modal: true },
    "Remove Room",
  );
  if (confirmed !== "Remove Room") return "cancelled";
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Removing ${selected.label}…`, cancellable: false },
    () => runJoshRoom(["rooms", "remove", selected.project.id, "--backend", "r2"], cwd),
  );
  await vscode.window.showInformationMessage(`Removed “${selected.label}”.`);
  await roomsProvider?.refresh();
  return "removed";
}

async function serveRoom(preferredProject) {
  const cwd = activeWorkspace();
  const marker = currentRoom(cwd);
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  let project = preferredProject || catalog.projects.find((item) => item.id === marker?.project_id);
  if (!project) {
    const selected = await vscode.window.showQuickPick(
      catalog.projects.map((item) => ({ label: item.display_name, project: item })),
      { title: "Josh: Serve Room Images", placeHolder: "Choose a Room", ignoreFocusOut: true },
    );
    if (!selected) return "cancelled";
    project = selected.project;
  }
  const history = await runJoshRoom(["snapshots", "list", project.id, "--backend", "r2"], cwd);
  let snapshotId = history.latest;
  if (history.snapshots.length > 1) {
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
  const terminal = vscode.window.createTerminal({ name: `Images: ${project.display_name}`, cwd });
  terminal.show(true);
  terminal.sendText(`josh-room serve ${project.id} --snapshot ${snapshotId} --backend r2`, true);
  return "started";
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
  outputChannel = vscode.window.createOutputChannel("Josh Room");
  roomsProvider = new RoomsProvider();
  const roomsView = vscode.window.createTreeView("joshRoom.rooms", { treeDataProvider: roomsProvider });
  context.subscriptions.push(outputChannel, roomsView);
  register(context, "joshRoom.save", saveRoom);
  register(context, "joshRoom.enter", enterRoom);
  register(context, "joshRoom.remove", removeRoom);
  register(context, "joshRoom.serve", serveRoom);
  register(context, "joshRoom.refresh", () => roomsProvider.refresh());
  roomsProvider.refresh().catch((error) => {
    outputChannel.appendLine(`error: ${error.message || String(error)}`);
  });
}

module.exports = { activate };
